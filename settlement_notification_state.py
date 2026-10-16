"""Shared, fail-closed codec for settlement notification receipts in History."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from typing import Any

from history_store import build_header_map, column_to_a1
from sheets_retry import retry_sheet_operation


ALLOWED_STATES = frozenset({"SENDING", "SENT", "FAILED", "DELIVERY_UNKNOWN"})


class NotificationStateError(RuntimeError):
    """A safe, non-financial explanation for an unreadable/conflicting marker."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(detail)
        self.reason_code = reason_code


def resolve_history_sheet(worksheets: list[Any], role_for: Any) -> Any:
    """Resolve the single History worksheet shared by send and recovery paths."""
    candidates = [worksheet for worksheet in worksheets if role_for(worksheet.title) == "HISTORY"]
    if len(candidates) != 1:
        reason = "NOTIFICATION_HISTORY_IDENTITY_AMBIGUOUS" if candidates else "NOTIFICATION_HISTORY_UNAVAILABLE"
        raise NotificationStateError(reason, reason)
    return candidates[0]


def _row_date(row: list[Any], date_column: int) -> str:
    if len(row) < date_column:
        return ""
    raw = str(row[date_column - 1] or "").strip()
    if not raw:
        return ""
    try:
        return dt.date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return raw[:10]


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return raw


def _decode_state(value: Any, *, row_number: int, key: str) -> dict[str, Any]:
    if isinstance(value, str):
        sent_at = _timestamp(value)
        if sent_at:
            return {"status": "SENT", "sentAt": sent_at, "legacy": True}
        raise NotificationStateError("NOTIFICATION_MARKER_UNKNOWN_STATE", f"row {row_number}: invalid state for {key}")
    if not isinstance(value, dict) or value.get("status") not in ALLOWED_STATES:
        raise NotificationStateError("NOTIFICATION_MARKER_UNKNOWN_STATE", f"row {row_number}: invalid state for {key}")
    version = value.get("schemaVersion")
    if version not in (None, 1, 2):
        raise NotificationStateError("NOTIFICATION_MARKER_UNKNOWN_VERSION", f"row {row_number}: unsupported state version for {key}")
    supplied_hash = value.get("markerHash")
    if supplied_hash is not None:
        unsigned = copy.deepcopy(value)
        unsigned.pop("markerHash", None)
        expected_hash = hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if supplied_hash != expected_hash:
            raise NotificationStateError("NOTIFICATION_MARKER_HASH_INVALID", f"row {row_number}: marker hash mismatch for {key}")
    return copy.deepcopy(value)


def _merge_candidates(candidates: list[tuple[int, dict[str, Any]]], *, date_key: str, window: str) -> dict[str, Any]:
    if not candidates:
        return {"status": "PENDING"}
    merged = copy.deepcopy(candidates[0][1])
    for row_number, candidate in candidates[1:]:
        if merged.get("status") != candidate.get("status"):
            rows = sorted({item[0] for item in candidates})
            raise NotificationStateError(
                "NOTIFICATION_MARKER_CONFLICT",
                f"rows {rows}: conflicting states for {date_key}|{window}",
            )
        for field in ("sentAt", "messageId", "attemptId", "attemptedAt", "sourceRunId", "sourceCommit"):
            left, right = merged.get(field), candidate.get(field)
            if left is not None and right is not None and str(left) != str(right):
                rows = sorted({item[0] for item in candidates})
                raise NotificationStateError(
                    "NOTIFICATION_MARKER_CONFLICT",
                    f"rows {rows}: conflicting {field} for {date_key}|{window}",
                )
            if left is None and right is not None:
                merged[field] = right
        merged["legacy"] = bool(merged.get("legacy") and candidate.get("legacy"))
    merged["storageRows"] = sorted({row_number for row_number, _ in candidates})
    return merged


def _columns(rows: list[list[Any]]) -> tuple[int, int]:
    if not rows:
        raise NotificationStateError("NOTIFICATION_HISTORY_UNAVAILABLE", "History header is missing")
    headers = build_header_map(rows[0])
    date_column = headers.get("Date")
    marker_column = headers.get("Settlement_Notification_Sent_At")
    if not date_column:
        raise NotificationStateError("NOTIFICATION_DATE_COLUMN_MISSING", "History Date column is missing")
    if not marker_column:
        raise NotificationStateError("NOTIFICATION_MARKER_COLUMN_MISSING", "notification marker column is missing")
    return date_column, marker_column


def read_state(history_sheet: Any, date_key: str, window: str) -> dict[str, Any]:
    if history_sheet is None:
        raise NotificationStateError("NOTIFICATION_STATE_UNAVAILABLE", "History worksheet is unavailable")
    rows = retry_sheet_operation("notification_state.get_all_values", history_sheet.get_all_values)
    date_column, marker_column = _columns(rows)
    candidates: list[tuple[int, dict[str, Any]]] = []
    date_window_key = f"{date_key}|{window}"
    date_legacy_key = f"{date_key}|legacy"

    for row_number, row in enumerate(rows[1:], start=2):
        raw = str(row[marker_column - 1] or "").strip() if len(row) >= marker_column else ""
        if not raw:
            continue
        row_date = _row_date(row, date_column)
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            legacy_time = _timestamp(raw)
            if legacy_time:
                if row_date == date_key:
                    candidates.append((row_number, {"status": "SENT", "sentAt": legacy_time, "legacy": True}))
                continue
            raise NotificationStateError("NOTIFICATION_MARKER_MALFORMED", f"row {row_number}: malformed marker format")

        if isinstance(decoded, str):
            legacy_time = _timestamp(decoded)
            if not legacy_time:
                raise NotificationStateError("NOTIFICATION_MARKER_MALFORMED", f"row {row_number}: malformed JSON timestamp")
            if row_date == date_key:
                candidates.append((row_number, {"status": "SENT", "sentAt": legacy_time, "legacy": True}))
            continue

        if not isinstance(decoded, dict):
            raise NotificationStateError("NOTIFICATION_MARKER_UNKNOWN_FORMAT", f"row {row_number}: marker must be an object")

        values: list[tuple[str, Any]] = []
        if row_date == date_key:
            if window in decoded:
                values.append((window, decoded[window]))
            if date_legacy_key in decoded:
                values.append((date_legacy_key, decoded[date_legacy_key]))
        if date_window_key in decoded:
            values.append((date_window_key, decoded[date_window_key]))
        for key, value in values:
            candidates.append((row_number, _decode_state(value, row_number=row_number, key=key)))

    return _merge_candidates(candidates, date_key=date_key, window=window)


def _read_marker_object(raw: str, row_date: str, row_number: int) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        sent_at = _timestamp(raw)
        if sent_at:
            return {f"{row_date}|legacy": {"status": "SENT", "sentAt": sent_at, "legacy": True}}
        raise NotificationStateError("NOTIFICATION_MARKER_MALFORMED", f"row {row_number}: malformed marker format")
    if isinstance(decoded, str):
        sent_at = _timestamp(decoded)
        if sent_at:
            return {f"{row_date}|legacy": {"status": "SENT", "sentAt": sent_at, "legacy": True}}
        raise NotificationStateError("NOTIFICATION_MARKER_MALFORMED", f"row {row_number}: malformed JSON timestamp")
    if not isinstance(decoded, dict):
        raise NotificationStateError("NOTIFICATION_MARKER_UNKNOWN_FORMAT", f"row {row_number}: marker must be an object")
    return copy.deepcopy(decoded)


def write_state(history_sheet: Any, date_key: str, window: str, state: dict[str, Any]) -> None:
    if history_sheet is None:
        raise NotificationStateError("NOTIFICATION_STATE_UNAVAILABLE", "History worksheet is unavailable")
    rows = retry_sheet_operation("notification_state.get_all_values", history_sheet.get_all_values)
    date_column, marker_column = _columns(rows)
    if len(rows) < 2:
        raise NotificationStateError("NOTIFICATION_HISTORY_ROW_MISSING", "History has no row to store a receipt")
    date_rows = [index for index, row in enumerate(rows[1:], start=2) if _row_date(row, date_column) == date_key]
    row_number = date_rows[-1] if date_rows else len(rows)
    row = rows[row_number - 1]
    row_date = _row_date(row, date_column)
    raw = str(row[marker_column - 1] or "").strip() if len(row) >= marker_column else ""
    markers = _read_marker_object(raw, row_date, row_number)
    target_key = f"{date_key}|{window}"
    existing = markers.get(target_key)
    if existing is None and row_date == date_key:
        existing = markers.get(window)
    if existing is not None:
        prior = _decode_state(existing, row_number=row_number, key=target_key)
        same_attempt = state.get("attemptId") and state.get("attemptId") == prior.get("attemptId")
        retryable_rate_limit = prior.get("status") == "FAILED" and prior.get("reasonCode") == "TELEGRAM_RATE_LIMITED" and state.get("status") == "SENDING"
        same_sent_receipt = prior.get("status") == state.get("status") == "SENT" and str(prior.get("messageId")) == str(state.get("messageId"))
        legal_transition = same_attempt and prior.get("status") == "SENDING" and state.get("status") in ALLOWED_STATES
        if not (retryable_rate_limit or same_sent_receipt or legal_transition or prior == state):
            raise NotificationStateError("NOTIFICATION_MARKER_CONFLICT", f"row {row_number}: refusing to replace existing {target_key}")

    record = {"schemaVersion": 2, "notificationType": "settlement", "windowDate": date_key, "window": window, **state}
    record["markerHash"] = hashlib.sha256(
        json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    markers[target_key] = record
    retry_sheet_operation(
        "notification_state.update",
        history_sheet.update,
        f"{column_to_a1(marker_column)}{row_number}",
        [[json.dumps(markers, ensure_ascii=False, separators=(",", ":"))]],
    )


__all__ = ["NotificationStateError", "read_state", "resolve_history_sheet", "write_state"]
