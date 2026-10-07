"""Versioned, point-in-time Beta policy and research-price helpers.

The dashboard consumes a human-approved legacy policy or a policy carrying
verified automatic-validation evidence. Candidate research stays separate
until source quality and live portfolio coverage pass. Prices are normalised
from raw closes plus explicit split/dividend events; provider ``Adj Close``
values are never accepted.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import re
import time
import threading
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from risk import estimate_beta_from_returns, quarterly_half_kelly


POLICY_SCHEMA_VERSION = 1
AUTO_VALIDATION_ALGORITHM = "quarterly-risk-v4"
AUTO_VALIDATION_REQUIRED_CHECKS = frozenset({
    "point_in_time_cutoff",
    "completed_week_window",
    "paired_week_observations",
    "corporate_action_response_evidence",
    "official_action_range_and_schema",
    "fx_timezone_quote_direction_and_coverage",
    "research_rows_cropped_to_point_in_time_window",
    "source_content_hash",
    "finite_coefficients",
    "kelly_formula",
})
MIN_WEEKLY_OBSERVATIONS = 104
DEFAULT_ACTIVE_POLICY_PATH = "config/beta-policy-active.json"
DEFAULT_ACTIVE_KELLY_PATH = "config/kelly-policy-active.json"
FIXED_BETAS = {"006208": 1.0, "00685L": 2.0}
OTC_SYMBOLS = {"00886", "3455"}

# One run may estimate many Taiwan holdings against the same official range
# tables. Share only successful responses inside an explicit run scope; never
# carry official event responses between settlements.
_OFFICIAL_QUERY_GUARD = threading.RLock()
_OFFICIAL_QUERY_CACHE: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[Any, str]] | None = None
_OFFICIAL_QUERY_LOCKS: dict[tuple[str, tuple[tuple[str, str], ...]], threading.Lock] = {}
_OFFICIAL_SOURCE_SEMAPHORE = threading.BoundedSemaphore(2)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10].replace("/", "-"))
    except ValueError:
        return None


def _symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _canonical_hash(payload: Any) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _auto_validation_error(payload: Mapping[str, Any]) -> str | None:
    validation = payload.get("autoValidation")
    if not isinstance(validation, Mapping):
        return "automatic validation evidence missing"
    if validation.get("algorithmVersion") != AUTO_VALIDATION_ALGORITHM:
        return "automatic validation algorithm mismatch"
    if str(validation.get("validationStatus", "")).upper() != "PASS":
        return "automatic validation did not pass"
    checks = validation.get("checks")
    if not isinstance(checks, (list, tuple)) or not AUTO_VALIDATION_REQUIRED_CHECKS.issubset(
        {str(item) for item in checks}
    ):
        return "automatic validation checks incomplete"
    if not str(validation.get("validatedBy", "")).strip():
        return "automatic validation source missing"
    try:
        validated_at = datetime.fromisoformat(str(validation.get("validatedAt", "")).replace("Z", "+00:00"))
    except ValueError:
        return "automatic validation timestamp invalid"
    if validated_at.tzinfo is None:
        return "automatic validation timestamp has no timezone"
    input_hash = str(validation.get("inputHash", ""))
    payload_hash = str(validation.get("payloadHash", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", input_hash) or not re.fullmatch(r"[0-9a-f]{64}", payload_hash):
        return "automatic validation hash missing"
    unsigned = dict(payload)
    unsigned_validation = dict(validation)
    unsigned_validation.pop("payloadHash", None)
    unsigned["autoValidation"] = unsigned_validation
    if _canonical_hash(unsigned) != payload_hash:
        return "automatic policy content hash mismatch"
    return None


def _valid_corporate_action_evidence(value: Any, *, window_start: date | None = None, window_end: date | None = None) -> bool:
    if not isinstance(value, Mapping):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("eventsHash", ""))):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("seriesHash", ""))):
        return False
    verification_status = str(value.get("verificationStatus", "")).upper()
    provider = str(value.get("provider", ""))
    if provider == "Yahoo Chart API":
        # Yahoo is acceptable for observed events, but an omitted/empty events
        # object is not proof that no actions occurred. Taiwan no-event claims
        # must be backed by a complete exchange query below.
        if verification_status != "EVENTS_VERIFIED" or value.get("eventMapPresent") is not True:
            return False
        try:
            if int(value.get("eventCount", 0)) <= 0:
                return False
        except (TypeError, ValueError):
            return False
    elif provider in {"TWSE official corporate-action API", "TPEx official corporate-action API"}:
        if verification_status not in {"EVENTS_VERIFIED", "NO_EVENTS_VERIFIED"}:
            return False
        if value.get("coverageComplete") is not True:
            return False
        expected_families = {"twt49u", "twtaau", "twtb8u", "twtcau"} if provider.startswith("TWSE") else {"exDailyQ", "revivt", "pvChgRslt"}
        if not expected_families.issubset({str(item) for item in value.get("sourceFamilies", [])}):
            return False
        response_hashes = value.get("sourceResponseHashes")
        if not isinstance(response_hashes, Mapping) or not expected_families.issubset(response_hashes):
            return False
        if any(not re.fullmatch(r"[0-9a-f]{64}", str(response_hashes.get(name, ""))) for name in expected_families):
            return False
        try:
            event_count = int(value.get("eventCount", -1))
        except (TypeError, ValueError):
            return False
        if event_count < 0 or (verification_status == "NO_EVENTS_VERIFIED" and event_count != 0):
            return False
        if verification_status == "EVENTS_VERIFIED" and event_count <= 0:
            return False
        requested_start = _as_date(value.get("requestedStart"))
        requested_end = _as_date(value.get("requestedEnd"))
        if requested_start is None or requested_end is None or requested_start > requested_end:
            return False
        if window_start is not None and requested_start > window_start:
            return False
        if window_end is not None and requested_end < window_end:
            return False
        try:
            verified_at = datetime.fromisoformat(str(value.get("verifiedAt", "")).replace("Z", "+00:00"))
        except ValueError:
            return False
        if verified_at.tzinfo is None:
            return False
    else:
        return False
    requested_events = value.get("requestedEvents")
    if not isinstance(requested_events, (list, tuple)) or not {"history", "splits", "dividends"}.issubset(
        {str(item).lower() for item in requested_events}
    ):
        return False
    requested_start = _as_date(value.get("requestedStart"))
    requested_end = _as_date(value.get("requestedEnd"))
    if requested_start is None or requested_end is None or requested_start > requested_end:
        return False
    if window_start is not None and requested_start > window_start:
        return False
    if window_end is not None and requested_end < window_end:
        return False
    try:
        verified_at = datetime.fromisoformat(str(value.get("verifiedAt", "")).replace("Z", "+00:00"))
    except ValueError:
        return False
    return verified_at.tzinfo is not None


def _parse_taiwan_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    western_compact = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text)
    if western_compact:
        try:
            return date(int(western_compact.group(1)), int(western_compact.group(2)), int(western_compact.group(3)))
        except ValueError:
            return None
    compact_roc = re.fullmatch(r"(\d{3})(\d{2})(\d{2})", text)
    if compact_roc:
        try:
            return date(int(compact_roc.group(1)) + 1911, int(compact_roc.group(2)), int(compact_roc.group(3)))
        except ValueError:
            return None
    roc_match = re.fullmatch(r"(\d{2,3})[年/](\d{1,2})[月/](\d{1,2})日?", text)
    if roc_match:
        try:
            return date(int(roc_match.group(1)) + 1911, int(roc_match.group(2)), int(roc_match.group(3)))
        except ValueError:
            return None
    return _as_date(text)


def _official_action_specs(market: str, start: date, end: date) -> tuple[str, list[tuple[str, str, dict[str, str]]]]:
    start_ymd, end_ymd = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    if market == "otc":
        roc_start = f"{start.year - 1911}/{start.month:02d}/{start.day:02d}"
        roc_end = f"{end.year - 1911}/{end.month:02d}/{end.day:02d}"
        return "TPEx official corporate-action API", [
            ("exDailyQ", "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ", {"startDate": roc_start, "endDate": roc_end}),
            ("revivt", "https://www.tpex.org.tw/www/zh-tw/bulletin/revivt", {"startDate": roc_start, "endDate": roc_end}),
            ("pvChgRslt", "https://www.tpex.org.tw/www/zh-tw/bulletin/pvChgRslt", {"startDate": roc_start, "endDate": roc_end}),
        ]
    return "TWSE official corporate-action API", [
        ("twt49u", "https://www.twse.com.tw/rwd/zh/exRight/TWT49U", {"startDate": start_ymd, "endDate": end_ymd, "response": "json"}),
        ("twtaau", "https://www.twse.com.tw/rwd/zh/reducation/TWTAUU", {"startDate": start_ymd, "endDate": end_ymd, "response": "json"}),
        ("twtb8u", "https://www.twse.com.tw/rwd/zh/change/TWTB8U", {"startDate": start_ymd, "endDate": end_ymd, "response": "json"}),
        ("twtcau", "https://www.twse.com.tw/rwd/zh/split/TWTCAU", {"startDate": start_ymd, "endDate": end_ymd, "response": "json"}),
    ]


def _official_rows(payload: Any, *, provider: str, family: str) -> list[tuple[dict[str, Any], str]]:
    """Parse only complete, recognizable official range-query responses."""
    if not isinstance(payload, Mapping):
        raise ValueError(f"{family}: official response is not an object")
    if provider.startswith("TWSE"):
        status = str(payload.get("stat", "")).strip()
        fields, data = payload.get("fields"), payload.get("data")
        if isinstance(fields, list) and isinstance(data, list):
            if status not in {"OK", "查詢日期範圍內無資料", "無符合條件資料"}:
                raise ValueError(f"{family}: official response status is not complete")
            if data and (
                not any("代號" in str(field) for field in fields)
                or not any("日期" in str(field) for field in fields)
            ):
                raise ValueError(f"{family}: official symbol/date fields missing")
            if not data and status == "OK" and (
                not fields
                or not any("代號" in str(field) for field in fields)
                or not any("日期" in str(field) for field in fields)
            ):
                raise ValueError(f"{family}: empty official response has no verifiable schema")
            rows = []
            for raw in data:
                if not isinstance(raw, (list, tuple)) or len(raw) != len(fields):
                    raise ValueError(f"{family}: official row schema mismatch")
                rows.append(({str(key): value for key, value in zip(fields, raw)}, status))
            return rows
        if status in {"查詢日期範圍內無資料", "無符合條件資料"}:
            return []
        raise ValueError(f"{family}: official fields/data missing")
    if str(payload.get("stat", "ok")).strip().lower() not in {"ok", "success"}:
        raise ValueError(f"{family}: TPEx response status is not complete")
    tables = payload.get("tables")
    if not isinstance(tables, list) or not tables:
        raise ValueError(f"{family}: TPEx tables missing")
    result = []
    recognized_table = False
    for table in tables:
        if not isinstance(table, Mapping):
            raise ValueError(f"{family}: TPEx table schema mismatch")
        fields, data = table.get("fields"), table.get("data")
        if not isinstance(fields, list) or not isinstance(data, list):
            raise ValueError(f"{family}: TPEx fields/data missing")
        if not fields:
            if data:
                raise ValueError(f"{family}: TPEx data has no field schema")
            continue
        field_names = [str(field) for field in fields]
        has_symbol = any(("代號" in field or "代碼" in field) for field in field_names)
        has_date = any("日期" in field for field in field_names)
        if not (has_symbol and has_date):
            if data:
                raise ValueError(f"{family}: TPEx symbol/date fields missing")
            continue
        recognized_table = True
        for raw in data:
            if not isinstance(raw, (list, tuple)) or len(raw) != len(fields):
                raise ValueError(f"{family}: TPEx row schema mismatch")
            result.append(({str(key): value for key, value in zip(field_names, raw)}, "OK"))
    if not recognized_table:
        raise ValueError(f"{family}: TPEx empty response has no verifiable schema")
    return result


def _official_period_matches(payload: Any, start: date, end: date) -> bool:
    """If an exchange echoes its query range, require an exact match."""
    if not isinstance(payload, Mapping) or payload.get("date") in (None, ""):
        return True
    raw = str(payload.get("date", "")).strip()
    parts = re.split(r"\s*(?:~|～|至)\s*", raw)
    if len(parts) != 2:
        return start == end and _parse_taiwan_date(raw) == start
    return _parse_taiwan_date(parts[0]) == start and _parse_taiwan_date(parts[1]) == end


def official_action_cache_scope():
    """Share identical official requests among symbols within one settlement."""
    from contextlib import contextmanager

    @contextmanager
    def scope():
        global _OFFICIAL_QUERY_CACHE, _OFFICIAL_QUERY_LOCKS
        with _OFFICIAL_QUERY_GUARD:
            _OFFICIAL_QUERY_CACHE = {}
            _OFFICIAL_QUERY_LOCKS = {}
        try:
            yield
        finally:
            with _OFFICIAL_QUERY_GUARD:
                _OFFICIAL_QUERY_CACHE = None
                _OFFICIAL_QUERY_LOCKS = {}

    return scope()


def _official_payload(getter: Any, url: str, params: Mapping[str, str], timeout: float) -> tuple[Any, str]:
    cache_key = (url, tuple(sorted((str(key), str(value)) for key, value in params.items())))
    with _OFFICIAL_QUERY_GUARD:
        cache = _OFFICIAL_QUERY_CACHE
        lock = _OFFICIAL_QUERY_LOCKS.setdefault(cache_key, threading.Lock()) if cache is not None else None
    if lock is not None:
        with lock:
            with _OFFICIAL_QUERY_GUARD:
                cached = _OFFICIAL_QUERY_CACHE.get(cache_key) if _OFFICIAL_QUERY_CACHE is not None else None
            if cached is not None:
                return cached
            payload = _request_official_json(getter, url, params, timeout)
            result = (payload, _canonical_hash(payload))
            with _OFFICIAL_QUERY_GUARD:
                if _OFFICIAL_QUERY_CACHE is not None:
                    _OFFICIAL_QUERY_CACHE[cache_key] = result
            return result
    payload = _request_official_json(getter, url, params, timeout)
    return payload, _canonical_hash(payload)


def _request_official_json(getter: Any, url: str, params: Mapping[str, str], timeout: float) -> Any:
    for attempt in range(3):
        try:
            with _OFFICIAL_SOURCE_SEMAPHORE:
                response = getter(url, params=dict(params), headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
                response.raise_for_status()
                return response.json()
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as error:
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            transient = isinstance(error, (requests.Timeout, requests.ConnectionError)) or status_code == 429 or (
                isinstance(status_code, int) and 500 <= status_code < 600
            )
            if not transient or attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError("official source request exhausted")


def fetch_official_taiwan_corporate_actions(
    symbol: str,
    *,
    market: str,
    start: date,
    end: date,
    http_get: Any = None,
    timeout: float = 12,
) -> dict[str, Any]:
    """Verify Taiwan actions over the full research window using exchange data.

    Cash dividends are supported in the total-return normalization. Stock
    rights, reductions, and par-value changes remain unavailable until their
    exact price-adjustment factor is independently supported.
    """
    getter = http_get or requests.get
    otc = _symbol(symbol) in OTC_SYMBOLS
    provider, specs = _official_action_specs("otc" if otc else "twse", start, end)
    matched: dict[str, list[dict[str, Any]]] = {family: [] for family, _, _ in specs}
    response_hashes: dict[str, str] = {}
    try:
        for family, url, params in specs:
            raw_payload, response_hash = _official_payload(getter, url, params, timeout)
            if not _official_period_matches(raw_payload, start, end):
                raise ValueError(f"{family}: official query range echo mismatch")
            parsed = _official_rows(raw_payload, provider=provider, family=family)
            response_hashes[family] = response_hash
            for row, _ in parsed:
                code_key = next((key for key in row if "代號" in key or "代碼" in key), None)
                event_key = next((key for key in row if "日期" in key), None)
                code = str(row.get(code_key, "")).strip() if code_key else ""
                if code != _symbol(symbol):
                    continue
                event_date = _parse_taiwan_date(row.get(event_key)) if event_key else None
                if event_date is None:
                    raise ValueError(f"{family}: matching symbol has invalid event date")
                if not start <= event_date <= end:
                    continue
                row["__eventDate"] = event_date.isoformat()
                matched[family].append(row)

        unsupported = [family for family, rows in matched.items() if rows and family not in {"twt49u", "exDailyQ", "twtcau"}]
        dividends: list[dict[str, Any]] = []
        splits: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        for row in matched.get("twt49u", []) + matched.get("exDailyQ", []):
            if "欄位" in row:
                fields = row["欄位"]
                field_map = {str(key): value for key, value in row.items() if not str(key).startswith("__")}
                cash_key = next((key for key in field_map if "現金股利" in key or key == "息值"), None)
                bonus_key = next((key for key in field_map if "無償配股" in key or "股票股利" in key), None)
                cash = _finite(str(field_map.get(cash_key, "")).replace(",", "")) if cash_key else None
                bonus_shares = _finite(str(field_map.get(bonus_key, "0")).replace(",", "")) if bonus_key else 0.0
                if cash is None or bonus_shares is None:
                    unsupported.append("exDailyQ_schema")
                    continue
                if bonus_shares > 0:
                    unsupported.append("stock_distribution")
                if cash > 0:
                    dividends.append({"date": row["__eventDate"], "amount": cash})
            else:
                event_kind = " ".join(str(value).strip() for key, value in row.items() if key in {"權/息", "權息", "種類"})
                right_key = next((key for key in row if key == "權值" or key.startswith("權值 ")), None)
                cash_key = next((key for key in row if key == "息值" or key.startswith("息值 ")), None)
                combined_key = next((key for key in row if "權值+息值" in key or "權值＋息值" in key), None)
                right_value = _finite(str(row.get(right_key, "0")).replace(",", "")) if right_key else 0.0
                cash_value = _finite(str(row.get(cash_key, "")).replace(",", "")) if cash_key else None
                if cash_value is None and combined_key and event_kind.strip() in {"息", "除息", "現金股利"}:
                    cash_value = _finite(str(row.get(combined_key, "")).replace(",", ""))
                if combined_key and any(marker in event_kind for marker in ("權", "股")):
                    right_value = 1.0
                if right_value is None or cash_value is None:
                    unsupported.append("twt49u_schema")
                    continue
                if right_value > 0 or any(marker in event_kind for marker in ("權", "股票")):
                    unsupported.append("stock_rights")
                if cash_value > 0:
                    dividends.append({"date": row["__eventDate"], "amount": cash_value})
            events.append(row)
        for row in matched.get("twtcau", []):
            fields = {str(key): value for key, value in row.items() if not str(key).startswith("__")}
            old_close_key = next((key for key in fields if "停止買賣前" in key and ("收盤" in key or "價格" in key)), None)
            resume_price_key = next((key for key in fields if "恢復買賣參考價" in key or "恢復買賣參考價格" in key), None)
            old_close = _finite(str(fields.get(old_close_key, "")).replace(",", "")) if old_close_key else None
            resume_price = _finite(str(fields.get(resume_price_key, "")).replace(",", "")) if resume_price_key else None
            if old_close is None or resume_price is None or old_close <= 0 or resume_price <= 0:
                unsupported.append("twtcau_split_ratio_unavailable")
                continue
            ratio = old_close / resume_price
            if not math.isfinite(ratio) or ratio <= 0:
                unsupported.append("twtcau_split_ratio_invalid")
                continue
            splits.append({"date": row["__eventDate"], "numerator": ratio, "denominator": 1.0})
            events.append(row)
        if unsupported:
            raise ValueError("unsupported official capital-action type")
        normalized_events = {
            "dividends": sorted(dividends, key=lambda item: item["date"]),
            "splits": sorted(splits, key=lambda item: item["date"]),
            "officialRows": events,
        }
        return {
            "status": "NO_EVENTS_VERIFIED" if not events else "EVENTS_VERIFIED",
            "provider": provider,
            "events": normalized_events,
            "sourceFamilies": sorted(response_hashes),
            "sourceResponseHashes": response_hashes,
            "coverageComplete": len(response_hashes) == len(specs),
            "requestedStart": start.isoformat(),
            "requestedEnd": end.isoformat(),
        }
    except Exception as error:  # noqa: BLE001 - callers retain a reason code, never a false pass
        message = str(error).lower()
        if "unsupported official" in message or "stock_rights" in message or "stock_distribution" in message:
            reason_code = "OFFICIAL_ACTION_TYPE_UNSUPPORTED"
        elif "schema" in message or "count" in message or "truncated" in message or "status is not complete" in message or "invalid event date" in message or "range echo mismatch" in message:
            reason_code = "OFFICIAL_ACTION_RESPONSE_INVALID"
        else:
            reason_code = "OFFICIAL_ACTION_SOURCE_UNAVAILABLE"
        return {"status": "UNVERIFIED", "provider": provider, "reasonCode": reason_code}


def yahoo_chart_symbols(symbol: str, market: str) -> tuple[str, ...]:
    """Return deterministic raw-chart symbols without yfinance state."""
    key = _symbol(symbol)
    if str(market).lower() in {"tw", "taiwan", "twd"}:
        preferred = "TWO" if key in OTC_SYMBOLS else "TW"
        alternate = "TW" if preferred == "TWO" else "TWO"
        return (f"{key}.{preferred}", f"{key}.{alternate}")
    if str(market).lower() in {"us", "usa", "usd"}:
        return (key,)
    return (key,)


def fetch_yahoo_research_series(
    symbol: str,
    *,
    market: str,
    start: date,
    end: date,
    http_get: Any = None,
    attempts: int = 3,
    timeout: float = 12,
    sleep_fn: Any = time.sleep,
) -> dict[str, Any]:
    """Fetch raw OHLC and explicit events for candidate construction.

    This endpoint is used only as a raw-history transport.  The returned
    contract never reads ``adjclose`` and always exposes a normalized research
    price generated by :func:`normalize_research_price`.
    """
    getter = http_get or requests.get
    failures: list[str] = []
    # Yahoo daily bars are timestamped against each instrument's exchange
    # timezone. Widen transport bounds, then crop on those local dates.
    period1 = int(datetime.combine(start - timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    period2 = int(datetime.combine(end + timedelta(days=3), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    chart_symbols = yahoo_chart_symbols(symbol, market)
    attempt_limit = max(1, attempts)
    per_symbol_attempts = (
        [attempt_limit - 1, 1]
        if len(chart_symbols) > 1 and attempt_limit > 1
        else [attempt_limit]
    )
    total_attempts = 0
    for chart_symbol, symbol_attempts in zip(chart_symbols, per_symbol_attempts):
        for attempt in range(symbol_attempts):
            try:
                total_attempts += 1
                response = getter(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{chart_symbol}",
                    params={"period1": period1, "period2": period2, "interval": "1d", "events": "history,splits,div"},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=timeout,
                )
                response.raise_for_status()
                payload = response.json()
                result = payload["chart"]["result"][0]
                meta = result.get("meta") or {}
                exchange_timezone = str(meta.get("exchangeTimezoneName") or "").strip()
                if not exchange_timezone:
                    raise ValueError("Yahoo exchange timezone metadata missing")
                try:
                    source_tz = ZoneInfo(exchange_timezone)
                except (ZoneInfoNotFoundError, ValueError):
                    raise ValueError("Yahoo exchange timezone metadata invalid")
                if (_symbol(symbol) == "TWD=X" or str(market).lower() == "fx") and str(meta.get("currency", "")).upper() != "TWD":
                    raise ValueError("USD/TWD quote currency mismatch")
                timestamps = result.get("timestamp") or []
                quote = (result.get("indicators") or {}).get("quote", [{}])[0]
                closes = quote.get("close") or []
                rows = []
                clipped_rows = 0
                seen_sessions: set[date] = set()
                for index, timestamp in enumerate(timestamps):
                    try:
                        item_date = datetime.fromtimestamp(float(timestamp), tz=timezone.utc).astimezone(source_tz).date()
                    except (TypeError, ValueError, OverflowError, OSError):
                        continue
                    if item_date < start or item_date > end:
                        clipped_rows += 1
                        continue
                    if item_date in seen_sessions:
                        raise ValueError(f"duplicate Yahoo session date {item_date.isoformat()}")
                    seen_sessions.add(item_date)
                    if index >= len(closes) or _finite(closes[index]) is None:
                        continue
                    open_value = _finite(quote.get("open", [None] * len(timestamps))[index]) if index < len(quote.get("open", [])) else None
                    high_value = _finite(quote.get("high", [None] * len(timestamps))[index]) if index < len(quote.get("high", [])) else None
                    low_value = _finite(quote.get("low", [None] * len(timestamps))[index]) if index < len(quote.get("low", [])) else None
                    close_value = _finite(closes[index])
                    if any(value is None or value <= 0 for value in (open_value, high_value, low_value, close_value)):
                        raise ValueError(f"invalid Yahoo OHLC values for {item_date.isoformat()}")
                    if low_value > high_value or low_value > min(open_value, close_value) or high_value < max(open_value, close_value):
                        raise ValueError(f"inconsistent Yahoo OHLC values for {item_date.isoformat()}")
                    rows.append({
                        "date": item_date.isoformat(),
                        "open": open_value,
                        "high": high_value,
                        "low": low_value,
                        "close": close_value,
                    })
                if _symbol(symbol) == "TWD=X" or str(market).lower() == "fx":
                    fx_rows = normalize_research_price(rows)
                    if not fx_rows:
                        raise ValueError("empty normalized FX series")
                    series_hash = _canonical_hash(fx_rows)
                    return {
                        "symbol": "TWD=X",
                        "market": "fx",
                        "currency": "TWD_PER_USD",
                        "quotePair": "USD/TWD",
                        "rows": fx_rows,
                        "source": f"Yahoo Chart raw FX ({chart_symbol})",
                        "seriesHash": series_hash,
                        "fxEvidence": {
                            "provider": "Yahoo Chart API",
                            "baseCurrency": "USD",
                            "quoteCurrency": "TWD",
                            "requestedStart": start.isoformat(),
                            "requestedEnd": end.isoformat(),
                            "verifiedAt": datetime.now(timezone.utc).isoformat(),
                            "coverageComplete": True,
                            "sourceHash": series_hash,
                            "exchangeTimezoneName": exchange_timezone,
                            "clippedOutOfRangeRows": clipped_rows,
                        },
                        "status": "READY",
                        "attempts": total_attempts,
                    }
                raw_events = result.get("events")
                events = raw_events if isinstance(raw_events, Mapping) else {}
                splits = []
                for item in (events.get("splits") or {}).values():
                    if not isinstance(item, Mapping):
                        raise ValueError("invalid split event evidence")
                    event_timestamp = _finite(item.get("date"))
                    numerator, denominator = _finite(item.get("numerator")), _finite(item.get("denominator"))
                    if event_timestamp is None or numerator is None or denominator is None or numerator <= 0 or denominator <= 0:
                        raise ValueError("invalid split event evidence")
                    event_date = datetime.fromtimestamp(event_timestamp, tz=timezone.utc).astimezone(source_tz).date()
                    if not start <= event_date <= end:
                        continue
                    splits.append({
                        "date": event_date.isoformat(),
                        "numerator": numerator,
                        "denominator": denominator,
                    })
                dividends = []
                for item in (events.get("dividends") or {}).values():
                    if not isinstance(item, Mapping):
                        raise ValueError("invalid dividend event evidence")
                    event_timestamp = _finite(item.get("date"))
                    amount = _finite(item.get("amount"))
                    if event_timestamp is None or amount is None or amount < 0:
                        raise ValueError("invalid dividend event evidence")
                    event_date = datetime.fromtimestamp(event_timestamp, tz=timezone.utc).astimezone(source_tz).date()
                    if not start <= event_date <= end:
                        continue
                    dividends.append({
                        "date": event_date.isoformat(),
                        "amount": amount,
                    })
                event_count = len(splits) + len(dividends)
                official_evidence = None
                is_taiwan = str(market).lower() in {"tw", "taiwan", "twd"}
                if is_taiwan:
                    official_evidence = fetch_official_taiwan_corporate_actions(
                        symbol, market="otc" if _symbol(symbol) in OTC_SYMBOLS else "twse",
                        start=start, end=end, http_get=getter, timeout=timeout,
                    )
                    if official_evidence.get("status") in {"EVENTS_VERIFIED", "NO_EVENTS_VERIFIED"}:
                        official_events = official_evidence["events"]
                        dividends = official_events.get("dividends", [])
                        splits = official_events.get("splits", [])
                        action_payload = official_events
                    else:
                        action_payload = dict(events)
                else:
                    action_payload = {"splits": splits, "dividends": dividends}
                normalized = normalize_research_price(rows, splits=splits, dividends=dividends)
                if not normalized:
                    raise ValueError("empty normalized research series")
                series_hash = _canonical_hash(normalized)
                corporate_evidence = None
                if official_evidence and official_evidence.get("status") in {"EVENTS_VERIFIED", "NO_EVENTS_VERIFIED"}:
                    events_hash = _canonical_hash(action_payload)
                    corporate_evidence = {
                        "provider": official_evidence["provider"],
                        "verificationStatus": official_evidence["status"],
                        "coverageComplete": official_evidence["coverageComplete"],
                        "sourceFamilies": official_evidence["sourceFamilies"],
                        "sourceResponseHashes": official_evidence["sourceResponseHashes"],
                        "eventCount": len(action_payload.get("officialRows", [])),
                        "eventsHash": events_hash,
                        "seriesHash": series_hash,
                        "requestedStart": start.isoformat(),
                        "requestedEnd": end.isoformat(),
                        "verifiedAt": datetime.now(timezone.utc).isoformat(),
                        "requestedEvents": ["history", "splits", "dividends"],
                    }
                elif event_count and isinstance(raw_events, Mapping):
                    events_hash = _canonical_hash(action_payload)
                    corporate_evidence = {
                        "provider": "Yahoo Chart API",
                        "verificationStatus": "EVENTS_VERIFIED",
                        "eventMapPresent": True,
                        "eventsHash": events_hash,
                        "seriesHash": series_hash,
                        "requestedStart": start.isoformat(),
                        "requestedEnd": end.isoformat(),
                        "verifiedAt": datetime.now(timezone.utc).isoformat(),
                        "requestedEvents": ["history", "splits", "dividends"],
                        "eventCount": event_count,
                        "eventCounts": {"splits": len(splits), "dividends": len(dividends)},
                    }
                return {
                    "symbol": _symbol(symbol),
                    "market": str(market).lower(),
                    "currency": "USD" if str(market).lower() in {"us", "usa", "usd"} else "TWD",
                    "rows": normalized,
                    "corporateActionEvents": action_payload,
                    "source": f"Yahoo Chart raw OHLC ({chart_symbol}) + research-price contract v1",
                    "corporateActionStatus": "PASS" if corporate_evidence else "UNAVAILABLE",
                    "corporateActionEvidence": corporate_evidence,
                    "seriesHash": series_hash,
                    "reason": None if corporate_evidence else (official_evidence or {}).get("reason", "corporate action evidence unavailable"),
                    "reasonCode": None if corporate_evidence else (official_evidence or {}).get("reasonCode", "CORPORATE_ACTION_EVIDENCE_UNAVAILABLE"),
                    "attempts": total_attempts,
                }
            except Exception as error:  # noqa: BLE001 - diagnostic boundary
                failures.append(f"{chart_symbol}:{type(error).__name__}")
                if isinstance(error, ValueError):
                    return {
                        "symbol": _symbol(symbol),
                        "market": str(market).lower(),
                        "currency": "USD" if str(market).lower() in {"us", "usa", "usd"} else "TWD",
                        "rows": [],
                        "source": None,
                        "corporateActionStatus": "UNAVAILABLE",
                        "status": "UNAVAILABLE",
                        "reason": str(error),
                        "reasonCode": "SOURCE_DATA_INVALID",
                        "attempts": total_attempts,
                    }
                if attempt + 1 < max(1, attempts):
                    sleep_fn(min(2 ** attempt, 8))
    return {
        "symbol": _symbol(symbol),
        "market": str(market).lower(),
        "currency": "USD" if str(market).lower() in {"us", "usa", "usd"} else "TWD",
        "rows": [],
        "source": None,
        "corporateActionStatus": "UNAVAILABLE",
        "status": "UNAVAILABLE",
        "reason": "; ".join(failures) or "history unavailable",
        "reasonCode": "SOURCE_DATA_INVALID" if failures and all(item.endswith(":ValueError") for item in failures) else "SOURCE_UNAVAILABLE",
    }


def fetch_finmind_research_series(
    symbol: str,
    *,
    token: str | None,
    start: date,
    end: date,
    http_get: Any = None,
    timeout: float = 12,
) -> dict[str, Any] | None:
    """Fetch Taiwan raw OHLC through the authenticated research source.

    ``None`` means the caller should use its secondary raw-chart provider;
    malformed or empty responses are explicit unavailable contracts.
    """
    if not token:
        return None
    getter = http_get or requests.get
    try:
        response = getter(
            "https://api.finmindtrade.com/api/v4/data",
            params={
                "dataset": "TaiwanStockPrice",
                "data_id": _symbol(symbol),
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "token": token,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        records = response.json().get("data", [])
        rows = [
            {
                "date": item.get("date"),
                "open": item.get("open"),
                "high": item.get("max"),
                "low": item.get("min"),
                "close": item.get("close"),
            }
            for item in records
            if isinstance(item, Mapping)
        ]
        normalized = normalize_research_price(rows)
        return {
            "symbol": _symbol(symbol),
            "market": "tw",
            "currency": "TWD",
            "rows": normalized,
            "source": "FinMind TaiwanStockPrice raw OHLC + research-price contract v1",
            # FinMind's price endpoint does not return the split/dividend
            # evidence required by the automatic quarterly policy. The
            # selector will use the raw-chart fallback for research inputs.
            "corporateActionStatus": "UNAVAILABLE",
            "attempts": 1,
        }
    except Exception as error:  # noqa: BLE001 - source boundary
        return {
            "symbol": _symbol(symbol),
            "market": "tw",
            "currency": "TWD",
            "rows": [],
            "source": None,
            "corporateActionStatus": "UNAVAILABLE",
            "status": "UNAVAILABLE",
            "reason": type(error).__name__,
        }


def fetch_research_series(symbol: str, *, market: str, start: date, end: date, token: str | None = None, http_get: Any = None) -> dict[str, Any]:
    """Apply the primary Taiwan source and raw-chart fallback policy."""
    if _symbol(symbol) == "TWD=X" or str(market).lower() == "fx":
        return fetch_yahoo_research_series("TWD=X", market="fx", start=start, end=end, http_get=http_get)
    if str(market).lower() in {"tw", "taiwan", "twd"} and token:
        primary = fetch_finmind_research_series(symbol, token=token, start=start, end=end, http_get=http_get)
        if primary and primary.get("rows") and primary.get("corporateActionStatus") == "PASS":
            return primary
    return fetch_yahoo_research_series(symbol, market=market, start=start, end=end, http_get=http_get)


def _quarter_index(value: date) -> int:
    return value.year * 4 + (value.month - 1) // 3


def _quarter_label(index: int) -> str:
    year, zero_based_quarter = divmod(index, 4)
    return f"{year:04d}Q{zero_based_quarter + 1}"


def _parse_quarter_label(value: Any) -> int | None:
    match = re.fullmatch(r"(\d{4})Q([1-4])", str(value or "").strip().upper())
    return int(match.group(1)) * 4 + int(match.group(2)) - 1 if match else None


def _policy_quarter_lifecycle(payload: Mapping[str, Any], cutoff: date, today: date) -> tuple[str, str | None]:
    cutoff_quarter = _quarter_index(cutoff)
    effective = _parse_quarter_label(payload.get("effectiveFromQuarter"))
    if effective is None:
        effective = cutoff_quarter + 1
    if effective != cutoff_quarter + 1:
        return "INVALID", "policy effective quarter does not follow its data cutoff"
    reference_through = _parse_quarter_label(payload.get("referenceThroughQuarter"))
    if reference_through is None:
        reference_through = effective + 1
    if reference_through < effective:
        return "INVALID", "policy reference period ends before its effective quarter"
    current = _quarter_index(today)
    if current < effective:
        return "INVALID", "policy is not yet effective"
    if current == effective:
        return "CURRENT", None
    if current <= reference_through:
        return "STALE_REFERENCE", "policy is stale"
    return "EXPIRED", "policy is stale"


def previous_quarter_cutoff(value: date | None = None) -> date:
    """Return the last calendar day of the quarter before ``value``."""
    current = value or date.today()
    quarter_start_month = ((current.month - 1) // 3) * 3 + 1
    quarter_start = date(current.year, quarter_start_month, 1)
    return quarter_start - timedelta(days=1)


def last_completed_week_cutoff(value: date | str | None = None) -> date:
    cutoff = _as_date(value) or previous_quarter_cutoff()
    return cutoff - timedelta(days=(cutoff.weekday() - 4) % 7)


def _event_rows(events: Any, *, value_keys: tuple[str, ...]) -> dict[date, float]:
    result: dict[date, float] = {}
    if isinstance(events, Mapping):
        iterable = events.values()
    elif isinstance(events, (list, tuple)):
        iterable = events
    else:
        iterable = []
    for item in iterable:
        if not isinstance(item, Mapping):
            continue
        event_date = _as_date(item.get("date") or item.get("exDate") or item.get("ex_date"))
        if event_date is None:
            raw_timestamp = item.get("timestamp")
            try:
                event_date = datetime.fromtimestamp(float(raw_timestamp), tz=timezone.utc).date()
            except (TypeError, ValueError, OverflowError, OSError):
                event_date = None
        if event_date is None:
            continue
        raw_value = next((item.get(key) for key in value_keys if item.get(key) is not None), None)
        number = _finite(raw_value)
        if number is not None:
            result[event_date] = number
    return result


def normalize_research_price(
    rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    *,
    splits: Any = None,
    dividends: Any = None,
) -> list[dict[str, Any]]:
    """Create split-adjusted and point-in-time total-return prices.

    Input rows must contain a date and a raw executable ``close``.  Split
    factors are applied only to dates before the event; dividends are added on
    the event date when building the total-return index.  Conflicting dates or
    invalid prices are rejected instead of being silently overwritten.
    """
    split_events = _event_rows(splits, value_keys=("ratio", "factor", "splitRatio"))
    if isinstance(splits, Mapping):
        split_events = {}
        for item in splits.values():
            if not isinstance(item, Mapping):
                continue
            event_date = _as_date(item.get("date") or item.get("exDate"))
            numerator = _finite(item.get("numerator"))
            denominator = _finite(item.get("denominator"))
            if event_date is not None and numerator and denominator:
                split_events[event_date] = denominator / numerator
            elif event_date is not None:
                ratio = _finite(item.get("ratio") or item.get("factor") or item.get("splitRatio"))
                if ratio and ratio > 0:
                    split_events[event_date] = ratio
    dividend_events = _event_rows(dividends, value_keys=("amount", "dividend", "cashAmount", "value"))

    by_date: dict[date, tuple[float, Mapping[str, Any]]] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        item_date = _as_date(row.get("date"))
        close = _finite(row.get("close"))
        if item_date is None or close is None or close <= 0:
            continue
        if item_date in by_date and abs(by_date[item_date][0] / close - 1) > 1e-9:
            raise ValueError(f"conflicting close for {item_date.isoformat()}")
        by_date[item_date] = (close, row)
    if not by_date:
        raise ValueError("research series has no valid raw closes")

    result: list[dict[str, Any]] = []
    total_return: float | None = None
    previous_adjusted: float | None = None
    for item_date in sorted(by_date):
        raw_close = by_date[item_date][0]
        split_factor = 1.0
        for event_date, factor in split_events.items():
            if item_date < event_date:
                split_factor *= factor
        adjusted_close = raw_close * split_factor
        if previous_adjusted is None:
            total_return = adjusted_close
        else:
            dividend = dividend_events.get(item_date, 0.0)
            total_return *= (adjusted_close + dividend) / previous_adjusted
        previous_adjusted = adjusted_close
        result.append({
            "date": item_date.isoformat(),
            "open": _finite(by_date[item_date][1].get("open")),
            "high": _finite(by_date[item_date][1].get("high")),
            "low": _finite(by_date[item_date][1].get("low")),
            "close": raw_close,
            "splitAdjustedClose": adjusted_close,
            "totalReturnIndex": total_return,
        })
    return result


def _weekly_rows(rows: list[Mapping[str, Any]], *, price_key: str, currency: str = "TWD", fx_rows: list[Mapping[str, Any]] | None = None, cutoff: date | None = None) -> dict[tuple[int, int], tuple[date, float]]:
    fx: dict[date, float] = {}
    for row in fx_rows or []:
        item_date = _as_date(row.get("date"))
        value = _finite(row.get("close", row.get("price")))
        if item_date is not None and value is not None and value > 0:
            fx[item_date] = value
    result: dict[tuple[int, int], tuple[date, float]] = {}
    for row in rows or []:
        item_date = _as_date(row.get("date"))
        value = _finite(row.get(price_key))
        if item_date is None or value is None or value <= 0 or (cutoff and item_date > cutoff):
            continue
        if currency.upper() != "TWD":
            candidates = [item for item in fx if item <= item_date]
            if not candidates:
                continue
            fx_date = max(candidates)
            if (item_date - fx_date).days > 7:
                continue
            value *= fx[fx_date]
        key = item_date.isocalendar()[:2]
        previous = result.get(key)
        if previous is None or item_date > previous[0]:
            result[key] = (item_date, value)
    return result


def weekly_research_series(
    rows: list[Mapping[str, Any]],
    *,
    price_key: str = "splitAdjustedClose",
    currency: str = "TWD",
    fx_rows: list[Mapping[str, Any]] | None = None,
    cutoff: date | str | None = None,
) -> list[dict[str, Any]]:
    """Return one completed, point-in-time research price per ISO week.

    Beta and Kelly must not accidentally consume daily rows as if they were
    weekly observations.  Keeping this conversion in the research-price
    layer makes the sampling frequency explicit and re-usable by both
    candidate builders.
    """
    cutoff_date = _as_date(cutoff)
    weekly = _weekly_rows(
        rows or [],
        price_key=price_key,
        currency=currency,
        fx_rows=fx_rows,
        cutoff=cutoff_date,
    )
    return [
        {"date": item_date.isoformat(), "close": value}
        for item_date, value in sorted(weekly.values(), key=lambda item: item[0])
    ]


def estimate_beta_policy(
    histories: Mapping[str, Mapping[str, Any]],
    benchmark_rows: list[Mapping[str, Any]],
    *,
    fx_rows: list[Mapping[str, Any]] | None = None,
    cutoff: date | str | None = None,
    min_observations: int = MIN_WEEKLY_OBSERVATIONS,
) -> dict[str, Any]:
    """Build a point-in-time candidate from contiguous, validated weekly data."""
    cutoff_date = _as_date(cutoff) or previous_quarter_cutoff()
    # A quarter is complete only after the Friday close. Exclude a partial
    # final week when the calendar quarter ends earlier in the week.
    completed_week_cutoff = cutoff_date - timedelta(days=(cutoff_date.weekday() - 4) % 7)
    window_start = completed_week_cutoff - timedelta(days=3 * 365 + 1)
    benchmark = _weekly_rows(benchmark_rows, price_key="splitAdjustedClose", cutoff=completed_week_cutoff)
    assets: dict[str, Any] = {}
    for raw_symbol, record in (histories or {}).items():
        symbol = _symbol(raw_symbol)
        if not symbol or symbol in FIXED_BETAS:
            continue
        rows = record.get("rows") if isinstance(record, Mapping) else None
        currency = str(record.get("currency", "TWD")) if isinstance(record, Mapping) else "TWD"
        weekly = _weekly_rows(rows or [], price_key="splitAdjustedClose", currency=currency, fx_rows=fx_rows, cutoff=completed_week_cutoff)
        if currency.upper() != "TWD":
            fx_by_date = {
                _as_date(row.get("date")): _finite(row.get("close", row.get("price")))
                for row in fx_rows or []
                if _as_date(row.get("date")) is not None
            }
            unconverted_weekly = _weekly_rows(
                rows or [], price_key="splitAdjustedClose", currency="TWD", cutoff=completed_week_cutoff,
            )
            fx_missing = len(weekly) != len(unconverted_weekly)
            for item_date, _ in weekly.values():
                prior = [fx_date for fx_date, value in fx_by_date.items() if fx_date and fx_date <= item_date and value and value > 0]
                if not prior or (item_date - max(prior)).days > 7:
                    fx_missing = True
                    break
            if not fx_rows or fx_missing:
                assets[symbol] = {
                    "status": "INSUFFICIENT_EVIDENCE",
                    "reason": "verified USD/TWD observations do not cover paired weeks",
                    "observations": 0,
                    "windowStart": window_start.isoformat(),
                    "windowEnd": completed_week_cutoff.isoformat(),
                    "corporateActionStatus": record.get("corporateActionStatus", "UNAVAILABLE") if isinstance(record, Mapping) else "UNAVAILABLE",
                    "corporateActionEvidence": record.get("corporateActionEvidence") if isinstance(record, Mapping) else None,
                    "source": record.get("source") if isinstance(record, Mapping) else None,
                }
                continue
        common = [
            key for key in sorted(set(benchmark).intersection(weekly))
            if window_start <= benchmark[key][0] <= completed_week_cutoff
        ]
        latest_pair_fresh = False
        if common:
            latest_key = common[-1]
            benchmark_latest = benchmark[latest_key][0]
            asset_latest = weekly[latest_key][0]
            latest_pair_fresh = (
                timedelta(0) <= completed_week_cutoff - benchmark_latest <= timedelta(days=10)
                and timedelta(0) <= completed_week_cutoff - asset_latest <= timedelta(days=10)
            )
        pair_start = max(benchmark[common[0]][0], weekly[common[0]][0]) if common else window_start
        pair_end = min(benchmark[common[-1]][0], weekly[common[-1]][0]) if common else completed_week_cutoff
        asset_returns, benchmark_returns = [], []
        for previous_key, current_key in zip(common, common[1:]):
            previous_date, previous_asset = weekly[previous_key]
            current_date, current_asset = weekly[current_key]
            previous_benchmark_date = benchmark[previous_key][0]
            current_benchmark_date = benchmark[current_key][0]
            previous_benchmark = benchmark[previous_key][1]
            current_benchmark = benchmark[current_key][1]
            # Do not disguise a skipped market week as one weekly return.
            if not (
                0 < (current_date - previous_date).days <= 10
                and 0 < (current_benchmark_date - previous_benchmark_date).days <= 10
            ):
                continue
            asset_returns.append(current_asset / previous_asset - 1)
            benchmark_returns.append(current_benchmark / previous_benchmark - 1)
        estimate = estimate_beta_from_returns(asset_returns, benchmark_returns, min_observations=min_observations)
        corporate_status = str(record.get("corporateActionStatus", "UNAVAILABLE")) if isinstance(record, Mapping) else "UNAVAILABLE"
        corporate_evidence = record.get("corporateActionEvidence") if isinstance(record, Mapping) else None
        source = str(record.get("source", "")) if isinstance(record, Mapping) else ""
        evidence_ok = _valid_corporate_action_evidence(
            corporate_evidence,
            window_start=pair_start,
            window_end=pair_end,
        )
        if estimate.get("status") != "READY" or not latest_pair_fresh or corporate_status != "PASS" or not evidence_ok or not source:
            if not latest_pair_fresh:
                reason = "latest paired observation is stale"
            else:
                reason = (
                    (record.get("reasonCode") or record.get("reason"))
                    if isinstance(record, Mapping) and (corporate_status != "PASS" or not evidence_ok)
                    else None
                ) or estimate.get("reason") or "corporate action or source evidence unavailable"
            assets[symbol] = {
                "status": "INSUFFICIENT_EVIDENCE",
                "reason": reason,
                "observations": estimate.get("observations", len(asset_returns)),
                "windowStart": pair_start.isoformat(),
                "windowEnd": pair_end.isoformat(),
                "corporateActionStatus": corporate_status,
                "corporateActionEvidence": corporate_evidence,
                "source": source or None,
            }
            continue
        assets[symbol] = {
            "status": "CANDIDATE",
            "beta": round(float(estimate["beta"]), 8),
            "observations": int(estimate["observations"]),
            "windowStart": pair_start.isoformat(),
            "windowEnd": pair_end.isoformat(),
            "method": "paired_weekly_split_adjusted_twd_returns",
            "corporateActionStatus": corporate_status,
            "corporateActionEvidence": corporate_evidence,
            "source": source,
            "market": str(record.get("market", "other")).lower(),
        }
    serial = json.dumps(assets, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "schemaVersion": POLICY_SCHEMA_VERSION,
        "status": "CANDIDATE" if any(item.get("status") == "CANDIDATE" for item in assets.values()) else "INSUFFICIENT_EVIDENCE",
        "approvalStatus": "AUTOMATIC_CANDIDATE",
        "policyVersion": f"candidate-{cutoff_date.isoformat()}",
        "benchmark": "006208.TW/TWD",
        "dataCutoff": cutoff_date.isoformat(),
        "effectiveFromQuarter": _quarter_label(_quarter_index(cutoff_date) + 1),
        "referenceThroughQuarter": _quarter_label(_quarter_index(cutoff_date) + 2),
        "assets": assets,
        "contentHash": hashlib.sha256(serial.encode("utf-8")).hexdigest(),
    }


def validate_active_policy_document(payload: Mapping[str, Any], *, as_of: date | None = None, min_observations: int = MIN_WEEKLY_OBSERVATIONS, allow_stale_reference: bool = False) -> tuple[dict[str, float], dict[str, Any] | None, str | None]:
    """Validate a human-approved legacy or automatically validated policy."""
    if not isinstance(payload, Mapping) or payload.get("schemaVersion") != POLICY_SCHEMA_VERSION:
        return {}, None, "policy schema version mismatch"
    approval_status = str(payload.get("approvalStatus", "")).upper()
    if str(payload.get("status", "")).upper() != "ACTIVE" or approval_status not in {"APPROVED", "AUTO_VALIDATED"}:
        return {}, None, "policy is not validated and active"
    if approval_status == "AUTO_VALIDATED":
        auto_error = _auto_validation_error(payload)
        if auto_error:
            return {}, None, auto_error
    cutoff = _as_date(payload.get("dataCutoff"))
    today = as_of or date.today()
    if cutoff is None or cutoff > previous_quarter_cutoff(today):
        return {}, None, "policy cutoff is not point-in-time"
    lifecycle, lifecycle_error = _policy_quarter_lifecycle(payload, cutoff, today)
    if lifecycle == "STALE_REFERENCE" and allow_stale_reference:
        lifecycle_error = None
    if lifecycle_error:
        return {}, None, lifecycle_error
    if lifecycle == "STALE_REFERENCE" and not allow_stale_reference:
        return {}, None, "policy is stale"
    if lifecycle == "EXPIRED":
        return {}, None, "policy is stale"
    assets = payload.get("assets")
    if not isinstance(assets, Mapping):
        return {}, None, "policy assets missing"
    canonical_assets = json.dumps(assets, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    expected_hash = hashlib.sha256(canonical_assets.encode("utf-8")).hexdigest()
    supplied_hash = str(payload.get("contentHash") or "")
    if supplied_hash and supplied_hash != expected_hash:
        return {}, None, "policy content hash mismatch"
    if approval_status == "AUTO_VALIDATED" and not supplied_hash:
        return {}, None, "automatically validated Beta content hash missing"
    betas: dict[str, float] = {}
    for raw_symbol, record in assets.items():
        symbol = _symbol(raw_symbol)
        if symbol in FIXED_BETAS:
            continue
        if not isinstance(record, Mapping):
            return {}, None, f"invalid policy record for {symbol}"
        record_status = str(record.get("status", "CANDIDATE" if approval_status == "APPROVED" else "")).upper()
        if record_status == "INSUFFICIENT_EVIDENCE":
            continue
        if record_status not in {"CANDIDATE", "AUTO_VALIDATED", "READY"}:
            return {}, None, f"invalid Beta status for {symbol}"
        beta = _finite(record.get("beta"))
        observations = record.get("observations")
        if beta is None or beta < 0 or not isinstance(observations, int) or observations < min_observations:
            return {}, None, f"invalid Beta evidence for {symbol}"
        if str(record.get("corporateActionStatus", "")).upper() != "PASS":
            return {}, None, f"corporate action not validated for {symbol}"
        if approval_status == "AUTO_VALIDATED":
            window_start = _as_date(record.get("windowStart"))
            window_end = _as_date(record.get("windowEnd"))
            if window_start is None or window_end is None or window_end > last_completed_week_cutoff(cutoff):
                return {}, None, f"Beta point-in-time window invalid for {symbol}"
            if not _valid_corporate_action_evidence(
                record.get("corporateActionEvidence"),
                window_start=window_start,
                window_end=window_end,
            ):
                return {}, None, f"corporate action evidence invalid for {symbol}"
        if not str(record.get("source", "")).strip():
            return {}, None, f"source missing for {symbol}"
        betas[symbol] = beta
    metadata = {
        "policyVersion": str(payload.get("policyVersion") or ""),
        "dataCutoff": cutoff.isoformat(),
        "contentHash": str(payload.get("contentHash") or ""),
        "benchmark": str(payload.get("benchmark") or "006208.TW/TWD"),
        "effectiveFromQuarter": _quarter_label(_parse_quarter_label(payload.get("effectiveFromQuarter")) or (_quarter_index(cutoff) + 1)),
        "referenceThroughQuarter": _quarter_label(_parse_quarter_label(payload.get("referenceThroughQuarter")) or (_quarter_index(cutoff) + 2)),
        "lifecycle": lifecycle,
        "approvalStatus": approval_status,
        "algorithmVersion": (payload.get("autoValidation") or {}).get("algorithmVersion") if isinstance(payload.get("autoValidation"), Mapping) else "legacy-manual",
    }
    return betas, metadata, None


def load_active_beta_policy(path: str | Path | None = None, *, as_of: date | None = None) -> dict[str, Any]:
    policy_path = Path(path or DEFAULT_ACTIVE_POLICY_PATH)
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "UNAVAILABLE", "quality": "policy_missing", "reason": "active Beta policy unavailable", "betas": {}, "metadata": None}
    betas, metadata, error = validate_active_policy_document(payload, as_of=as_of)
    if error:
        reference_betas, reference_metadata, reference_error = validate_active_policy_document(
            payload, as_of=as_of, allow_stale_reference=True,
        )
        if error == "policy is stale" and not reference_error:
            reference_metadata = {**(reference_metadata or {}), "lifecycle": "STALE_REFERENCE"}
            return {"status": "UNAVAILABLE", "quality": "policy_stale_reference", "reason": error,
                    "betas": {}, "metadata": None,
                    "referenceBetas": {**FIXED_BETAS, **reference_betas},
                    "referenceMetadata": reference_metadata}
        return {"status": "UNAVAILABLE", "quality": "policy_invalid", "reason": error, "betas": {}, "metadata": None,
                "referenceBetas": {}, "referenceMetadata": None}
    return {"status": "READY", "quality": "policy_complete", "reason": None, "betas": {**FIXED_BETAS, **betas}, "metadata": metadata}


def validate_active_kelly_document(payload: Mapping[str, Any], *, as_of: date | None = None, allow_stale_reference: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a human-approved legacy or automatically validated Kelly policy."""
    if not isinstance(payload, Mapping) or payload.get("schemaVersion") != POLICY_SCHEMA_VERSION:
        return None, "Kelly policy schema version mismatch"
    approval_status = str(payload.get("approvalStatus", "")).upper()
    if str(payload.get("status", "")).upper() != "ACTIVE" or approval_status not in {"APPROVED", "AUTO_VALIDATED"}:
        return None, "Kelly policy is not validated and active"
    if approval_status == "AUTO_VALIDATED":
        auto_error = _auto_validation_error(payload)
        if auto_error:
            return None, auto_error.replace("policy", "Kelly policy")
    cutoff = _as_date(payload.get("dataCutoff"))
    today = as_of or date.today()
    if cutoff is None or cutoff > previous_quarter_cutoff(today):
        return None, "Kelly policy cutoff is not point-in-time"
    lifecycle, lifecycle_error = _policy_quarter_lifecycle(payload, cutoff, today)
    if lifecycle == "STALE_REFERENCE" and allow_stale_reference:
        lifecycle_error = None
    if lifecycle_error:
        return None, lifecycle_error.replace("policy", "Kelly policy")
    if lifecycle == "STALE_REFERENCE" and not allow_stale_reference:
        return None, "Kelly policy is stale"
    if lifecycle == "EXPIRED":
        return None, "Kelly policy is stale"
    mu = _finite(payload.get("mu"))
    sigma = _finite(payload.get("sigma"))
    limit = _finite(payload.get("halfKellyLimit"))
    if mu is None or sigma is None or limit is None or mu <= 0 or sigma <= 0 or limit <= 0:
        return None, "Kelly policy parameters are invalid"
    expected = quarterly_half_kelly(mu, sigma)
    if expected.get("status") != "CANDIDATE" or abs(float(expected["halfKellyLimit"]) - limit) > 1e-8:
        return None, "Kelly policy formula mismatch"
    if approval_status == "AUTO_VALIDATED":
        window_start = _as_date(payload.get("windowStart"))
        window_end = _as_date(payload.get("windowEnd"))
        if window_start is None or window_end is None or window_end > last_completed_week_cutoff(cutoff):
            return None, "Kelly point-in-time window invalid"
        if not _valid_corporate_action_evidence(
            payload.get("corporateActionEvidence"),
            window_start=window_start,
            window_end=window_end,
        ):
            return None, "Kelly corporate action evidence invalid"
    return {
        "activeVersion": str(payload.get("activeVersion") or payload.get("policyVersion") or ""),
        "mu": float(expected["mu"]),
        "sigma": float(expected["sigma"]),
        "halfKellyLimit": float(expected["halfKellyLimit"]),
        "dataCutoff": cutoff.isoformat(),
        "approvalStatus": approval_status,
        "freshness": "current",
        "source": str(payload.get("source") or ""),
        "contentHash": str(payload.get("contentHash") or ""),
        "effectiveFromQuarter": _quarter_label(_parse_quarter_label(payload.get("effectiveFromQuarter")) or (_quarter_index(cutoff) + 1)),
        "referenceThroughQuarter": _quarter_label(_parse_quarter_label(payload.get("referenceThroughQuarter")) or (_quarter_index(cutoff) + 2)),
        "lifecycle": lifecycle,
        "algorithmVersion": (payload.get("autoValidation") or {}).get("algorithmVersion") if isinstance(payload.get("autoValidation"), Mapping) else "legacy-manual",
    }, None


def load_active_kelly_policy(path: str | Path | None = None, *, as_of: date | None = None) -> dict[str, Any]:
    policy_path = Path(path or DEFAULT_ACTIVE_KELLY_PATH)
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "UNAVAILABLE", "quality": "policy_missing", "reason": "active Kelly policy unavailable", "policy": None}
    policy, error = validate_active_kelly_document(payload, as_of=as_of)
    if error:
        reference, reference_error = validate_active_kelly_document(
            payload, as_of=as_of, allow_stale_reference=True,
        )
        if error == "Kelly policy is stale" and not reference_error:
            reference = {**(reference or {}), "lifecycle": "STALE_REFERENCE", "freshness": "stale_reference"}
            return {"status": "UNAVAILABLE", "quality": "policy_stale_reference", "reason": error,
                    "policy": None, "referencePolicy": reference}
        return {"status": "UNAVAILABLE", "quality": "policy_invalid", "reason": error, "policy": None,
                "referencePolicy": None}
    return {"status": "READY", "quality": "policy_complete", "reason": None, "policy": policy}
