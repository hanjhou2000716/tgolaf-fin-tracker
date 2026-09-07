"""Unified quote and TAIEX history contracts.

The production Buy&Hold signal needs completed TAIEX sessions, not a live
quote.  ``get_taiex_history`` keeps that contract independent from yfinance:
TWSE's documented monthly history endpoint is the primary source and Yahoo's
Chart API is a bounded fallback when the primary provider is unavailable.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import time
from typing import Any, Callable, Iterable, Mapping

import requests

from quotes import get_tw_stock_price


TAIEX_TWSE_URL = "https://www.twse.com.tw/indicesReport/MI_5MINS_HIST"
TAIEX_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
TAIEX_MIN_SESSIONS = 240
TAIEX_MAX_MONTHS = 36
TAIEX_MAX_AGE_DAYS = 7


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
            item_date = datetime.fromtimestamp(float(timestamp), tz=timezone.utc).date() if timestamp is not None else None
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
            deduped[item_date.isoformat()] = {"date": item_date.isoformat(), "close": close}
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


def fetch_twse_taiex_history(*, http_get: Callable[..., Any] | None = None, now: datetime | date | None = None, max_months: int = TAIEX_MAX_MONTHS, sleep_fn: Callable[[float], Any] = time.sleep) -> list[dict[str, Any]]:
    """Fetch enough monthly TWSE history to calculate DD240."""
    http_get = http_get or requests.get
    anchor = (now.date() if isinstance(now, datetime) else now) or datetime.now(timezone.utc).date()
    rows: list[dict[str, Any]] = []
    for index, month_start in enumerate(_month_starts(anchor, max_months)):
        response = http_get(
            TAIEX_TWSE_URL,
            params={"response": "json", "date": month_start.strftime("%Y%m01")},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        response.raise_for_status()
        rows.extend(parse_twse_taiex_payload(response.json()))
        if index + 1 < max_months and len(_dedupe_history(rows)) >= TAIEX_MIN_SESSIONS:
            break
        if index + 1 < max_months:
            sleep_fn(1.0)
    normalized = _dedupe_history(rows)
    if len(normalized) < TAIEX_MIN_SESSIONS:
        raise TaiexDataUnavailableError(f"TWSE history < {TAIEX_MIN_SESSIONS} sessions")
    return normalized


def fetch_yahoo_taiex_history(*, http_get: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
    """Fetch TAIEX history from Yahoo Chart API without yfinance state."""
    http_get = http_get or requests.get
    response = http_get(
        TAIEX_YAHOO_URL,
        params={"interval": "1d", "range": "2y"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    response.raise_for_status()
    normalized = _dedupe_history(parse_yahoo_taiex_payload(response.json()))
    if len(normalized) < TAIEX_MIN_SESSIONS:
        raise TaiexDataUnavailableError(f"Yahoo history < {TAIEX_MIN_SESSIONS} sessions")
    return normalized


def compare_taiex_sources(primary: Iterable[Mapping[str, Any]], secondary: Iterable[Mapping[str, Any]], *, max_relative_difference: float = 0.005) -> dict[str, Any]:
    """Return a non-financial comparison result for two normalized sources."""
    left, right = _dedupe_history(primary), _dedupe_history(secondary)
    if not left or not right:
        return {"status": "UNAVAILABLE", "reason": "source has no rows"}
    left_last, right_last = left[-1], right[-1]
    try:
        relative_difference = abs(float(left_last["close"]) / float(right_last["close"]) - 1.0)
    except (TypeError, ValueError, ZeroDivisionError):
        relative_difference = None
    if left_last["date"] != right_last["date"]:
        return {"status": "SOURCE_MISMATCH", "reason": "latest session date differs", "primaryDate": left_last["date"], "secondaryDate": right_last["date"]}
    if relative_difference is None or relative_difference > max_relative_difference:
        return {"status": "SOURCE_MISMATCH", "reason": "latest close differs beyond tolerance", "relativeDifference": relative_difference}
    return {"status": "MATCH", "latestSessionDate": left_last["date"], "relativeDifference": relative_difference}


def _history_is_fresh(rows: list[dict[str, Any]], now: datetime | date | None) -> bool:
    if not rows:
        return False
    anchor = (now.date() if isinstance(now, datetime) else now) or datetime.now(timezone.utc).date()
    latest = _parse_taiex_date(rows[-1].get("date"))
    return latest is not None and latest <= anchor and (anchor - latest).days <= TAIEX_MAX_AGE_DAYS


def get_taiex_history(*, now: datetime | date | None = None, http_get: Callable[..., Any] | None = None, twse_fetcher: Callable[[], list[dict[str, Any]]] | None = None, yahoo_fetcher: Callable[[], list[dict[str, Any]]] | None = None, sleep_fn: Callable[[float], Any] = time.sleep, compare_sources: bool = False) -> dict[str, Any]:
    """Return a validated TAIEX history contract for production signals.

    The primary-success path deliberately does not call Yahoo unless the
    caller opts into the secondary source check.  This keeps unit callers and
    lightweight runs deterministic while the production pipeline can request
    a same-run comparison and fail closed on a material mismatch.
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    primary_error = None
    try:
        primary = (twse_fetcher or (lambda: fetch_twse_taiex_history(http_get=http_get, now=now, sleep_fn=sleep_fn)))()
        primary = _dedupe_history(primary)
        if len(primary) >= TAIEX_MIN_SESSIONS and _history_is_fresh(primary, now):
            source_comparison: dict[str, Any] = {"status": "NOT_RUN", "reason": "primary source accepted"}
            if compare_sources:
                try:
                    secondary = (yahoo_fetcher or (lambda: fetch_yahoo_taiex_history(http_get=http_get)))()
                    source_comparison = compare_taiex_sources(primary, secondary)
                    if source_comparison.get("status") == "SOURCE_MISMATCH":
                        return {
                            "status": "UNAVAILABLE",
                            "history": [],
                            "source": None,
                            "quality": "source_mismatch",
                            "latestSessionDate": primary[-1]["date"],
                            "sessionCount": len(primary),
                            "fetchedAt": fetched_at,
                            "fallbackReason": "TWSE/Yahoo source mismatch",
                            "sourceComparison": source_comparison,
                        }
                except Exception as error:
                    # The secondary check is diagnostic only.  A healthy TWSE
                    # primary remains usable when Yahoo itself is unavailable.
                    source_comparison = {"status": "SECONDARY_UNAVAILABLE", "reason": f"Yahoo {type(error).__name__}"}
            return {
                "status": "READY",
                "history": primary,
                "source": "TWSE",
                "quality": "fresh",
                "latestSessionDate": primary[-1]["date"],
                "sessionCount": len(primary),
                "fetchedAt": fetched_at,
                "fallbackReason": None,
                "sourceComparison": source_comparison,
            }
        primary_error = "TWSE history unavailable, incomplete, or stale"
    except Exception as error:
        primary_error = f"TWSE {type(error).__name__}"

    try:
        fallback = (yahoo_fetcher or (lambda: fetch_yahoo_taiex_history(http_get=http_get)))()
        fallback = _dedupe_history(fallback)
        if len(fallback) < TAIEX_MIN_SESSIONS or not _history_is_fresh(fallback, now):
            raise TaiexDataUnavailableError("Yahoo history unavailable, incomplete, or stale")
        return {
            "status": "READY",
            "history": fallback,
            "source": "Yahoo Chart",
            "quality": "fresh",
            "latestSessionDate": fallback[-1]["date"],
            "sessionCount": len(fallback),
            "fetchedAt": fetched_at,
            "fallbackReason": primary_error,
            "sourceComparison": {"status": "NOT_RUN", "reason": "fallback source used"},
        }
    except Exception as error:
        return {
            "status": "UNAVAILABLE",
            "history": [],
            "source": None,
            "quality": "unavailable",
            "latestSessionDate": None,
            "sessionCount": 0,
            "fetchedAt": fetched_at,
            "fallbackReason": f"{primary_error}; Yahoo {type(error).__name__}",
            "sourceComparison": {"status": "NOT_RUN"},
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
