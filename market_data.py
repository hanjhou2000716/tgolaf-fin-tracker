"""Unified quote and TAIEX history contracts.

The production Buy&Hold signal needs completed TAIEX sessions, not a live
quote.  ``get_taiex_history`` keeps that contract independent from yfinance:
TWSE's documented monthly history endpoint is the primary source and Yahoo's
Chart API is a bounded fallback when the primary provider is unavailable.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

import requests

from quotes import get_tw_stock_price


TAIEX_TWSE_URL = "https://www.twse.com.tw/indicesReport/MI_5MINS_HIST"
TAIEX_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
TAIEX_MIN_SESSIONS = 240
TAIEX_MAX_MONTHS = 36
TAIEX_MAX_AGE_DAYS = 7
TAIEX_REQUEST_TIMEOUT_SECONDS = 15
TAIEX_MAX_ATTEMPTS = 3
TAIEX_TOTAL_TIMEOUT_SECONDS = 180
TAIEX_CACHE_SCHEMA_VERSION = "taiex-history-cache-v1"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
TAIPEI_CLOSE_HOUR = 14


class TaiexDataUnavailableError(RuntimeError):
    """Raised internally when a provider cannot return usable TAIEX rows."""


def _parse_taiex_date(value: Any) -> date | None:
    """Parse TWSE Gregorian/ROC dates and provider ISO values."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip().replace("年", "/").replace("月", "/").replace("日", "")
    if not text:
        return None
    text = text.replace("-", "/").replace(".", "/")
    parts = [part.strip() for part in text.split("/") if part.strip()]
    try:
        if len(parts) == 3:
            year, month, day = (int(part) for part in parts)
            if year < 1911:
                year += 1911
            return date(year, month, day)
        compact = text.replace("/", "")
        number = int(compact)
        if len(compact) == 7 and compact.startswith("1"):
            return date(number // 10000 + 1911, (number // 100) % 100, number % 100)
        if len(compact) == 8:
            return date(number // 10000, (number // 100) % 100, number % 100)
    except (TypeError, ValueError):
        return None
    return None


def _parse_taiex_number(value: Any) -> float | None:
    try:
        number = float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _taipei_now(value: datetime | date | None = None) -> datetime:
    """Return an aware Taiwan-time clock for session completion decisions."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=TAIPEI_TZ)
        return value.astimezone(TAIPEI_TZ)
    if isinstance(value, date):
        # A date-only test/caller means that date is a completed-session
        # anchor; use end-of-day rather than treating it as pre-open midnight.
        return datetime.combine(value, datetime.max.time(), tzinfo=TAIPEI_TZ)
    return datetime.now(TAIPEI_TZ)


def _completed_cutoff_date(value: datetime | date | None = None) -> date:
    """Return the latest date whose Taiwan close may safely be used.

    TWSE's regular close is complete after 14:00 Taiwan time.  Before that
    point the current date is excluded even if a provider has already exposed
    a partial/last-trade row.
    """
    clock = _taipei_now(value)
    cutoff = clock.date()
    if clock.hour < TAIPEI_CLOSE_HOUR:
        cutoff -= timedelta(days=1)
    return cutoff


def _filter_completed_sessions(rows: Iterable[Mapping[str, Any]], *, now: datetime | date | None = None) -> list[dict[str, Any]]:
    cutoff = _completed_cutoff_date(now)
    normalized = _dedupe_history(rows)
    return [
        row for row in normalized
        if (item_date := _parse_taiex_date(row["date"])) is not None
        and item_date <= cutoff
        and item_date.weekday() < 5
    ]


def parse_twse_taiex_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize one TWSE MI_5MINS_HIST monthly response.

    TWSE may return Chinese or English field labels.  Only the explicit date
    and close fields are accepted; arbitrary cell guessing is intentionally
    not allowed.
    """
    rows = payload.get("data") if isinstance(payload, Mapping) else None
    fields = payload.get("fields") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or not isinstance(fields, list):
        raise TaiexDataUnavailableError("TWSE payload missing fields/data")
    normalized_fields = [str(field).strip().lower() for field in fields]
    date_index = next((i for i, field in enumerate(normalized_fields) if field in {"日期", "date", "日期(年/月/日)"}), None)
    close_index = next((i for i, field in enumerate(normalized_fields) if field in {"收盤指數", "收盤指數 ", "close", "close index", "closing index"}), None)
    if date_index is None or close_index is None:
        raise TaiexDataUnavailableError("TWSE payload missing explicit date/close fields")
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) <= max(date_index, close_index):
            continue
        item_date = _parse_taiex_date(row[date_index])
        close = _parse_taiex_number(row[close_index])
        if item_date is not None and close is not None:
            result.append({"date": item_date.isoformat(), "close": close})
    if not result:
        raise TaiexDataUnavailableError("TWSE payload contained no valid TAIEX rows")
    return result


def parse_yahoo_taiex_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize Yahoo Chart API timestamps and close values."""
    try:
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        raise TaiexDataUnavailableError("Yahoo payload missing timestamp/close fields")
    rows: list[dict[str, Any]] = []
    for timestamp, raw_close in zip(timestamps or [], closes or []):
        close = _parse_taiex_number(raw_close)
        try:
            item_date = datetime.fromtimestamp(float(timestamp), tz=TAIPEI_TZ).date() if timestamp is not None else None
        except (TypeError, ValueError, OverflowError, OSError):
            item_date = None
        if item_date is not None and close is not None:
            rows.append({"date": item_date.isoformat(), "close": close})
    if not rows:
        raise TaiexDataUnavailableError("Yahoo payload contained no valid TAIEX rows")
    return rows


def _dedupe_history(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        item_date = _parse_taiex_date(row.get("date"))
        close = _parse_taiex_number(row.get("close"))
        if item_date is not None and close is not None:
            key = item_date.isoformat()
            previous = deduped.get(key)
            if previous is not None:
                previous_close = float(previous["close"])
                relative_difference = abs(close / previous_close - 1.0) if previous_close else math.inf
                if relative_difference > 1e-9:
                    raise TaiexDataUnavailableError(f"conflicting TAIEX close for {key}")
                continue
            deduped[key] = {"date": key, "close": close}
    return [deduped[key] for key in sorted(deduped)]


def _month_starts(anchor: date, months: int) -> list[date]:
    output: list[date] = []
    year, month = anchor.year, anchor.month
    for _ in range(max(1, months)):
        output.append(date(year, month, 1))
        month -= 1
        if month == 0:
            year -= 1
            month = 12
    return output


def _request_json_with_retry(
    http_get: Callable[..., Any],
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = TAIEX_REQUEST_TIMEOUT_SECONDS,
    sleep_fn: Callable[[float], Any] = time.sleep,
    deadline: float | None = None,
) -> Mapping[str, Any]:
    """Fetch JSON with bounded retries for transient provider failures."""
    last_error: Exception | None = None
    for attempt in range(TAIEX_MAX_ATTEMPTS):
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            response = http_get(url, params=params, headers=headers, timeout=timeout)
            status = getattr(response, "status_code", None)
            transient = status in {429, 500, 502, 503, 504}
            if transient:
                raise requests.HTTPError(f"HTTP {status}")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise TaiexDataUnavailableError("provider returned non-object JSON")
            return payload
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError, TaiexDataUnavailableError) as error:
            last_error = error
            status = getattr(locals().get("response"), "status_code", None)
            retryable = isinstance(error, (requests.Timeout, requests.ConnectionError)) or (
                isinstance(error, requests.HTTPError) and status in {429, 500, 502, 503, 504}
            )
            if isinstance(error, TaiexDataUnavailableError) and "non-object" not in str(error):
                retryable = False
            if not retryable or attempt + 1 >= TAIEX_MAX_ATTEMPTS:
                raise
            delay = min(4.0, 2 ** attempt)
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - time.monotonic()))
            if delay:
                sleep_fn(delay)
        except Exception as error:
            # Provider-specific mocks and malformed responses should fail the
            # provider path immediately; the other source can still be used.
            last_error = error
            raise
    raise TaiexDataUnavailableError(str(last_error or "provider request timed out"))


def fetch_twse_taiex_history(*, http_get: Callable[..., Any] | None = None, now: datetime | date | None = None, max_months: int = TAIEX_MAX_MONTHS, sleep_fn: Callable[[float], Any] = time.sleep, deadline: float | None = None) -> list[dict[str, Any]]:
    """Fetch enough monthly TWSE history to calculate DD240."""
    http_get = http_get or requests.get
    anchor = _completed_cutoff_date(now)
    deadline = deadline or (time.monotonic() + TAIEX_TOTAL_TIMEOUT_SECONDS)
    rows: list[dict[str, Any]] = []
    for index, month_start in enumerate(_month_starts(anchor, max_months)):
        payload = _request_json_with_retry(
            http_get,
            TAIEX_TWSE_URL,
            params={"response": "json", "date": month_start.strftime("%Y%m01")},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=TAIEX_REQUEST_TIMEOUT_SECONDS,
            sleep_fn=sleep_fn,
            deadline=deadline,
        )
        rows.extend(parse_twse_taiex_payload(payload))
        if index + 1 < max_months and len(_dedupe_history(rows)) >= TAIEX_MIN_SESSIONS:
            break
        if index + 1 < max_months:
            sleep_fn(1.0)
    normalized = _filter_completed_sessions(rows, now=now)
    if len(normalized) < TAIEX_MIN_SESSIONS:
        raise TaiexDataUnavailableError(f"TWSE history < {TAIEX_MIN_SESSIONS} sessions")
    return normalized


def fetch_yahoo_taiex_history(*, http_get: Callable[..., Any] | None = None, now: datetime | date | None = None, deadline: float | None = None) -> list[dict[str, Any]]:
    """Fetch TAIEX history from Yahoo Chart API without yfinance state."""
    http_get = http_get or requests.get
    payload = _request_json_with_retry(
        http_get,
        TAIEX_YAHOO_URL,
        params={"interval": "1d", "range": "2y"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=TAIEX_REQUEST_TIMEOUT_SECONDS,
        deadline=deadline,
    )
    normalized = _filter_completed_sessions(parse_yahoo_taiex_payload(payload), now=now)
    if len(normalized) < TAIEX_MIN_SESSIONS:
        raise TaiexDataUnavailableError(f"Yahoo history < {TAIEX_MIN_SESSIONS} sessions")
    return normalized


def compare_taiex_sources(primary: Iterable[Mapping[str, Any]], secondary: Iterable[Mapping[str, Any]], *, max_relative_difference: float = 0.005) -> dict[str, Any]:
    """Compare the most recent common completed session.

    A secondary provider can legitimately lag the official TWSE feed by one
    or more sessions.  Date lag alone is therefore diagnostic, not a data
    conflict; only a material close disagreement on a common session blocks
    the signal.
    """
    left, right = _dedupe_history(primary), _dedupe_history(secondary)
    if not left or not right:
        return {"status": "UNAVAILABLE", "reason": "source has no rows"}
    left_by_date = {row["date"]: row for row in left}
    right_by_date = {row["date"]: row for row in right}
    common_dates = sorted(set(left_by_date) & set(right_by_date))
    if not common_dates:
        return {
            "status": "NO_COMMON_SESSION",
            "reason": "sources have no common completed session",
            "primaryDate": left[-1]["date"],
            "secondaryDate": right[-1]["date"],
        }
    common_date = common_dates[-1]
    left_last, right_last = left_by_date[common_date], right_by_date[common_date]
    try:
        relative_difference = abs(float(left_last["close"]) / float(right_last["close"]) - 1.0)
    except (TypeError, ValueError, ZeroDivisionError):
        relative_difference = None
    if relative_difference is None or relative_difference > max_relative_difference:
        return {
            "status": "SOURCE_MISMATCH",
            "reason": "common session close differs beyond tolerance",
            "comparisonDate": common_date,
            "primaryDate": left[-1]["date"],
            "secondaryDate": right[-1]["date"],
            "relativeDifference": relative_difference,
        }
    if left[-1]["date"] != right[-1]["date"]:
        return {
            "status": "SECONDARY_LAGGING",
            "reason": "secondary source latest completed session lags primary",
            "comparisonDate": common_date,
            "primaryDate": left[-1]["date"],
            "secondaryDate": right[-1]["date"],
            "relativeDifference": relative_difference,
        }
    return {"status": "MATCH", "latestSessionDate": common_date, "comparisonDate": common_date, "relativeDifference": relative_difference}


def _history_is_fresh(rows: list[dict[str, Any]], now: datetime | date | None) -> bool:
    if not rows:
        return False
    anchor = _completed_cutoff_date(now)
    latest = _parse_taiex_date(rows[-1].get("date"))
    return latest is not None and latest <= anchor and (anchor - latest).days <= TAIEX_MAX_AGE_DAYS


def _history_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    canonical = json.dumps(_dedupe_history(rows), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_taiex_history_cache(result: Mapping[str, Any], *, now: datetime | date | None = None) -> dict[str, Any] | None:
    """Build the private, source-validated cache artifact for a READY result."""
    history = _filter_completed_sessions(result.get("history") or [], now=now)
    if result.get("status") != "READY" or not history or result.get("cacheUsed"):
        return None
    comparison = result.get("sourceComparison") or {}
    if comparison.get("status") == "SOURCE_MISMATCH":
        return None
    return {
        "schemaVersion": TAIEX_CACHE_SCHEMA_VERSION,
        "source": result.get("source"),
        "validatedAt": result.get("fetchedAt") or datetime.now(timezone.utc).isoformat(),
        "expectedLatestSessionDate": history[-1]["date"],
        "sessions": history,
        "sessionCount": len(history),
        "contentSha256": _history_digest(history),
    }


def validate_taiex_history_cache(payload: Mapping[str, Any], *, now: datetime | date | None = None) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Re-validate a downloaded cache before it can feed a signal.

    The strict cutoff equality intentionally fails closed when the official
    session calendar cannot be refreshed: a cache from yesterday cannot be
    mistaken for today's close.
    """
    if not isinstance(payload, Mapping) or payload.get("schemaVersion") != TAIEX_CACHE_SCHEMA_VERSION:
        return None, "cache schema version mismatch"
    try:
        history = _filter_completed_sessions(payload.get("sessions") or [], now=now)
        if len(history) < TAIEX_MIN_SESSIONS:
            return None, "cache has fewer than 240 completed sessions"
        if payload.get("sessionCount") != len(history):
            return None, "cache session count mismatch"
        if payload.get("expectedLatestSessionDate") != history[-1]["date"]:
            return None, "cache expected latest session mismatch"
        expected = _completed_cutoff_date(now).isoformat()
        if history[-1]["date"] != expected:
            return None, "cache does not cover latest expected completed session"
        if payload.get("contentSha256") != _history_digest(history):
            return None, "cache content digest mismatch"
    except (TypeError, ValueError, TaiexDataUnavailableError) as error:
        return None, f"cache validation failed: {type(error).__name__}"
    return history, None


def load_taiex_history_cache(path: str | os.PathLike[str] | None = None, *, now: datetime | date | None = None) -> tuple[list[dict[str, Any]] | None, str | None]:
    cache_path = Path(path or os.environ.get("TAIEX_HISTORY_CACHE_PATH", ".taiex-cache/taiex-history-cache.json"))
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "cache file unavailable"
    except (OSError, json.JSONDecodeError):
        return None, "cache file unreadable"
    return validate_taiex_history_cache(payload, now=now)


def write_taiex_history_cache(path: str | os.PathLike[str], result: Mapping[str, Any], *, now: datetime | date | None = None) -> bool:
    payload = build_taiex_history_cache(result, now=now)
    if payload is None:
        return False
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def get_taiex_history(
    *,
    now: datetime | date | None = None,
    http_get: Callable[..., Any] | None = None,
    twse_fetcher: Callable[[], list[dict[str, Any]]] | None = None,
    yahoo_fetcher: Callable[[], list[dict[str, Any]]] | None = None,
    sleep_fn: Callable[[float], Any] = time.sleep,
    compare_sources: bool = False,
    cache_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Return a validated TAIEX history contract for production signals.

    TWSE is authoritative when it has enough completed sessions.  Yahoo is a
    cross-check/fallback only; a lagging secondary date is not a conflict when
    the latest common completed close agrees.  A private cache is considered
    only after both providers fail and must cover the current completed-date
    cutoff exactly.
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    deadline = time.monotonic() + TAIEX_TOTAL_TIMEOUT_SECONDS
    expected_latest = _completed_cutoff_date(now).isoformat()
    primary_error: str | None = None
    secondary_error: str | None = None

    def ready(history: list[dict[str, Any]], source: str, *, raw_session_count: int | None = None, fallback_reason: str | None = None, comparison: Mapping[str, Any] | None = None, cache_used: bool = False, quality: str = "fresh") -> dict[str, Any]:
        return {
            "status": "READY",
            "marketDataStatus": "READY",
            "signalStatus": "READY",
            "history": history,
            "source": source,
            "quality": quality,
            "latestSessionDate": history[-1]["date"],
            "expectedLatestSessionDate": expected_latest,
            "completedSessionCutoff": expected_latest,
            "calendarValidation": "twse_session_rows" if source == "TWSE" else "strict_cutoff",
            "sessionCount": len(history),
            "rawSessionCount": raw_session_count if raw_session_count is not None else len(history),
            "completedSessionCount": len(history),
            "fetchedAt": fetched_at,
            "fallbackReason": fallback_reason,
            "sourceComparison": dict(comparison or {"status": "NOT_RUN"}),
            "cacheUsed": cache_used,
            "cacheValidation": "passed" if cache_used else "not_used",
        }

    try:
        primary_raw = _dedupe_history((twse_fetcher or (lambda: fetch_twse_taiex_history(http_get=http_get, now=now, sleep_fn=sleep_fn, deadline=deadline)))())
        primary_raw_count = len(primary_raw)
        primary = _filter_completed_sessions(primary_raw, now=now)
        if len(primary) >= TAIEX_MIN_SESSIONS and _history_is_fresh(primary, now):
            source_comparison: dict[str, Any] = {"status": "NOT_RUN", "reason": "primary source accepted"}
            if compare_sources:
                try:
                    secondary_raw = _dedupe_history((yahoo_fetcher or (lambda: fetch_yahoo_taiex_history(http_get=http_get, now=now, deadline=deadline)))())
                    secondary = _filter_completed_sessions(secondary_raw, now=now)
                    if len(secondary) < TAIEX_MIN_SESSIONS or not _history_is_fresh(secondary, now):
                        raise TaiexDataUnavailableError("Yahoo history incomplete or stale")
                    source_comparison = compare_taiex_sources(primary, secondary)
                    if source_comparison.get("status") == "SOURCE_MISMATCH":
                        return {
                            **ready(primary, "TWSE", raw_session_count=primary_raw_count, comparison=source_comparison),
                            "status": "UNAVAILABLE",
                            "marketDataStatus": "SOURCE_MISMATCH",
                            "signalStatus": "UNAVAILABLE",
                            "history": [],
                            "source": None,
                            "quality": "source_mismatch",
                            "fallbackReason": "TWSE/Yahoo common-session price mismatch",
                            "cacheUsed": False,
                            "cacheValidation": "not_allowed_after_source_mismatch",
                        }
                except Exception as error:
                    secondary_error = f"Yahoo {type(error).__name__}"
                    source_comparison = {"status": "SECONDARY_UNAVAILABLE", "reason": secondary_error}
            return ready(primary, "TWSE", raw_session_count=primary_raw_count, comparison=source_comparison)
        primary_error = "TWSE history unavailable, incomplete, or stale"
    except Exception as error:
        primary_error = f"TWSE {type(error).__name__}"

    try:
        if time.monotonic() >= deadline:
            raise TaiexDataUnavailableError("TAIEX provider timeout budget exhausted")
        fallback_raw = _dedupe_history((yahoo_fetcher or (lambda: fetch_yahoo_taiex_history(http_get=http_get, now=now, deadline=deadline)))())
        fallback_raw_count = len(fallback_raw)
        fallback = _filter_completed_sessions(fallback_raw, now=now)
        if len(fallback) < TAIEX_MIN_SESSIONS or not _history_is_fresh(fallback, now):
            raise TaiexDataUnavailableError("Yahoo history unavailable, incomplete, or stale")
        return ready(fallback, "Yahoo Chart", raw_session_count=fallback_raw_count, fallback_reason=primary_error, comparison={"status": "NOT_RUN", "reason": "fallback source used"})
    except Exception as error:
        secondary_error = f"Yahoo {type(error).__name__}"

    cache_history, cache_error = load_taiex_history_cache(cache_path, now=now)
    if cache_history is not None:
        return ready(
            cache_history,
            "TAIEX cache",
            raw_session_count=len(cache_history),
            fallback_reason=f"{primary_error}; {secondary_error}",
            comparison={"status": "CACHE_VALIDATED"},
            cache_used=True,
            quality="validated_cache",
        )
    return {
        "status": "UNAVAILABLE",
        "marketDataStatus": "UNAVAILABLE",
        "signalStatus": "UNAVAILABLE",
        "history": [],
        "source": None,
        "quality": "unavailable",
        "latestSessionDate": None,
        "expectedLatestSessionDate": expected_latest,
        "completedSessionCutoff": expected_latest,
        "calendarValidation": "strict_cutoff",
        "sessionCount": 0,
        "fetchedAt": fetched_at,
        "fallbackReason": f"{primary_error}; {secondary_error}; cache {cache_error}",
        "sourceComparison": {"status": "NOT_RUN"},
        "cacheUsed": False,
        "cacheValidation": cache_error,
    }


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    currency: str
    source: str
    as_of: str
    fetched_at: str
    is_stale: bool
    fallback_used: bool
    quality: str

    def as_dict(self):
        return self.__dict__.copy()


class MarketDataService:
    def __init__(self, *, ttl_minutes=90, now: Callable[[], datetime] | None = None):
        self.ttl = timedelta(minutes=ttl_minutes)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._cache: dict[str, Quote] = {}

    def _fresh(self, quote: Quote, now: datetime) -> bool:
        try:
            fetched = datetime.fromisoformat(quote.fetched_at.replace("Z", "+00:00"))
            return now - fetched <= self.ttl
        except ValueError:
            return False

    def get(self, symbol, *, currency, fetcher, source="provider") -> Quote:
        symbol = str(symbol).strip().upper()
        now = self._now()
        cached = self._cache.get(symbol)
        if cached and self._fresh(cached, now):
            return cached
        try:
            raw = fetcher(symbol)
            if isinstance(raw, Quote):
                quote = raw
            else:
                quote = Quote(
                    symbol=symbol,
                    price=float(raw),
                    currency=currency,
                    source=source,
                    as_of=now.isoformat(),
                    fetched_at=now.isoformat(),
                    is_stale=False,
                    fallback_used=False,
                    quality="ok",
                )
            self._cache[symbol] = quote
            return quote
        except Exception:
            if cached:
                stale = Quote(
                    **{**cached.as_dict(), "is_stale": True, "fallback_used": True, "quality": "stale"}
                )
                self._cache[symbol] = stale
                return stale
            raise

    def get_taiwan(self, symbol, finmind_token):
        return self.get(
            symbol,
            currency="TWD",
            source="FinMind/Yahoo/yfinance",
            fetcher=lambda item: get_tw_stock_price(item, finmind_token),
        )

    def get_fx(self, pair, fetcher, source="provider"):
        """Return the same quality-aware contract used for equity quotes."""
        return self.get(pair, currency="TWD", fetcher=fetcher, source=source)
