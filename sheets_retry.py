"""Bounded, auditable retries for Google Sheets API operations.

Google's Sheets service occasionally returns transient 429/5xx responses.
This module keeps retry policy in one place so callers can retry reads and
idempotent writes without ever logging worksheet values.  Appends are not
retried blindly; callers that use an append must re-read their idempotency key
before attempting it again.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

try:  # Keep lightweight History/validation tests usable without API extras.
    from gspread.exceptions import APIError as GoogleSheetsAPIError
except ModuleNotFoundError:  # pragma: no cover - production installs gspread
    class GoogleSheetsAPIError(Exception):
        """Fallback exception type used only when gspread is not installed."""


TRANSIENT_SHEETS_STATUS = frozenset({429, 500, 502, 503, 504})
DEFAULT_ATTEMPTS = 4
DEFAULT_BASE_DELAY_SECONDS = 2.0
DEFAULT_MAX_DELAY_SECONDS = 30.0
DEFAULT_MAX_TOTAL_SECONDS = 180.0

_operations: list[dict[str, Any]] = []
_fatal_operation: dict[str, Any] | None = None


def _status_code(error: BaseException) -> int | None:
    raw = getattr(getattr(error, "response", None), "status_code", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _retry_after(error: BaseException) -> float | None:
    headers = getattr(getattr(error, "response", None), "headers", None)
    if not headers:
        return None
    try:
        value = float(headers.get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        return None
    if value < 0:
        return None
    return min(value, DEFAULT_MAX_DELAY_SECONDS)


def _record(operation: str, *, attempts: int, statuses: list[int],
            success: bool, recovered: bool, error: BaseException | None = None) -> None:
    # Keep this deliberately non-financial: operation names and HTTP statuses
    # are sufficient to diagnose the availability boundary.
    item: dict[str, Any] = {
        "operation": str(operation),
        "attempts": int(attempts),
        "transientStatuses": list(statuses),
        "success": bool(success),
        "recovered": bool(recovered),
    }
    if error is not None:
        item["finalStatus"] = _status_code(error)
        item["errorType"] = type(error).__name__
    _operations.append(item)


def _write_summary_on_failure(operation: str, error: BaseException, attempts: int) -> None:
    # Failure paths can occur before dashboard_pipeline.main() gets a chance
    # to flush its summary.  Best-effort writing here gives Actions an
    # artifact while never masking the original error.
    path = os.getenv("SHEETS_OPERATION_SUMMARY_PATH", ".private-build/google-sheets-operation-summary.json")
    try:
        write_operation_summary(path, final_stage=operation, fatal_error=error, attempts=attempts)
    except Exception:
        pass


def retry_sheet_operation(
    operation: str,
    function: Callable[..., Any],
    *args: Any,
    attempts: int = DEFAULT_ATTEMPTS,
    sleep: Callable[[float], Any] = time.sleep,
    max_total_seconds: float = DEFAULT_MAX_TOTAL_SECONDS,
    **kwargs: Any,
) -> Any:
    """Run one Sheets operation with bounded retries for transient failures.

    Non-transient API errors and non-API exceptions are raised immediately.
    The final transient exception is re-raised after recording a safe summary.
    ``sleep`` is injectable for deterministic unit tests.
    """
    if attempts < 1:
        raise ValueError("attempts must be positive")
    global _fatal_operation
    started = time.monotonic()
    statuses: list[int] = []
    for attempt in range(1, attempts + 1):
        try:
            result = function(*args, **kwargs)
            _record(operation, attempts=attempt, statuses=statuses, success=True, recovered=attempt > 1)
            return result
        except GoogleSheetsAPIError as error:
            status = _status_code(error)
            if status is not None:
                statuses.append(status)
            transient = status in TRANSIENT_SHEETS_STATUS
            exhausted = attempt >= attempts
            elapsed = time.monotonic() - started
            if not transient or exhausted or elapsed >= max_total_seconds:
                _record(operation, attempts=attempt, statuses=statuses, success=False, recovered=False, error=error)
                _fatal_operation = {
                    "operation": str(operation),
                    "attempts": int(attempt),
                    "errorType": type(error).__name__,
                    "status": _status_code(error),
                }
                _write_summary_on_failure(operation, error, attempt)
                raise
            delay = _retry_after(error)
            if delay is None:
                delay = min(DEFAULT_BASE_DELAY_SECONDS * (2 ** (attempt - 1)), DEFAULT_MAX_DELAY_SECONDS)
            remaining = max_total_seconds - elapsed
            delay = min(delay, max(0.0, remaining))
            print(f"Google Sheets {operation} transient HTTP {status}; retry {attempt + 1}/{attempts} in {delay:g}s")
            sleep(delay)
        except Exception as error:
            _record(operation, attempts=attempt, statuses=statuses, success=False, recovered=False, error=error)
            _fatal_operation = {
                "operation": str(operation),
                "attempts": int(attempt),
                "errorType": type(error).__name__,
                "status": _status_code(error),
            }
            _write_summary_on_failure(operation, error, attempt)
            raise

    raise RuntimeError("unreachable retry loop")


def operation_summary() -> dict[str, Any]:
    return {
        "schemaVersion": "google-sheets-operation-summary-v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "operationCount": len(_operations),
        "operations": list(_operations),
    }


def write_operation_summary(
    path: str = ".private-build/google-sheets-operation-summary.json",
    *,
    final_stage: str | None = None,
    fatal_error: BaseException | None = None,
    attempts: int | None = None,
) -> None:
    """Write a non-financial operation summary for private Actions artifacts."""
    payload = operation_summary()
    if final_stage:
        payload["finalStage"] = str(final_stage)
    elif _fatal_operation:
        payload["finalStage"] = _fatal_operation["operation"]
    if attempts is not None:
        payload["finalAttempts"] = int(attempts)
    if fatal_error is not None:
        payload["status"] = "FAILED"
        payload["finalErrorType"] = type(fatal_error).__name__
        payload["finalStatus"] = _status_code(fatal_error)
    elif _fatal_operation is not None:
        payload["status"] = "FAILED"
        payload["finalErrorType"] = _fatal_operation["errorType"]
        payload["finalStatus"] = _fatal_operation["status"]
    else:
        payload["status"] = "OK"
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def reset_operation_summary() -> None:
    """Clear in-memory events (primarily useful for isolated tests)."""
    global _fatal_operation
    _operations.clear()
    _fatal_operation = None
