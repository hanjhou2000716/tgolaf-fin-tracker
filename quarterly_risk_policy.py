"""Automatic, point-in-time quarterly Beta and Kelly policy lifecycle."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping

from beta_policy import (
    AUTO_VALIDATION_ALGORITHM,
    FIXED_BETAS,
    _as_date,
    _canonical_hash,
    _valid_corporate_action_evidence,
    estimate_beta_policy,
    fetch_research_series,
    last_completed_week_cutoff,
    load_active_beta_policy,
    load_active_kelly_policy,
    official_action_cache_scope,
    previous_quarter_cutoff,
    validate_active_kelly_document,
    validate_active_policy_document,
    weekly_research_series,
)
from risk import build_quarterly_kelly_candidate, calculate_nav_beta


RESEARCH_REQUEST_BUDGET_SECONDS = 180
AUTO_VALIDATION_CHECKS = [
    "point_in_time_cutoff",
    "completed_week_window",
    "paired_week_observations",
    "corporate_action_response_evidence",
    "official_action_range_and_schema",
    "fx_timezone_quote_direction_and_coverage",
    "fx_source_specific_close_contract",
    "fx_window_limited_to_estimation_period",
    "research_rows_cropped_to_point_in_time_window",
    "source_content_hash",
    "finite_coefficients",
    "kelly_formula",
]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _quarter_label(value: date) -> str:
    return f"{value.year}Q{(value.month - 1) // 3 + 1}"


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return result if isinstance(result, dict) else None


def _sealed_cache(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(payload))
    result.pop("cacheHash", None)
    result["cacheHash"] = _canonical_hash(result)
    return result


def _valid_sealed_cache(payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(payload, Mapping):
        return False
    supplied = str(payload.get("cacheHash", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", supplied):
        return False
    unsigned = dict(payload)
    unsigned.pop("cacheHash", None)
    return _canonical_hash(unsigned) == supplied


def _history_is_valid(payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(payload, Mapping) or payload.get("status") not in {None, "READY"}:
        return False
    rows = payload.get("rows")
    evidence = payload.get("corporateActionEvidence")
    if not isinstance(rows, list) or not rows or not _valid_corporate_action_evidence(evidence):
        return False
    row_hash = _canonical_hash(rows)
    if row_hash != payload.get("seriesHash") or row_hash != evidence.get("seriesHash"):
        return False
    events = payload.get("corporateActionEvents")
    if not isinstance(events, Mapping) or _canonical_hash(events) != evidence.get("eventsHash"):
        return False
    return True


def _fx_history_is_valid(payload: Mapping[str, Any] | None, *, cutoff: date | None = None) -> bool:
    if not isinstance(payload, Mapping) or payload.get("status") not in {None, "READY"}:
        return False
    rows = payload.get("rows")
    evidence = payload.get("fxEvidence")
    if (
        payload.get("symbol") != "TWD=X"
        or payload.get("quotePair") != "USD/TWD"
        or payload.get("currency") != "TWD_PER_USD"
        or not isinstance(rows, list)
        or not rows
        or not isinstance(evidence, Mapping)
        or evidence.get("baseCurrency") != "USD"
        or evidence.get("quoteCurrency") != "TWD"
        or evidence.get("validationMethod") != "fx-close-v1"
    ):
        return False
    provider = str(evidence.get("provider", ""))
    if provider == "Yahoo Chart API":
        # Source-session dates are already normalized from Yahoo's own
        # exchangeTimezoneName. Do not assume a fixed timezone: it can vary
        # with the provider's instrument metadata.
        if not str(evidence.get("exchangeTimezoneName", "")).strip():
            return False
    elif provider == "Taiwan CBC OpenData":
        if evidence.get("closingConvention") != "Taiwan interbank daily close":
            return False
    else:
        return False
    try:
        requested_start = date.fromisoformat(str(evidence.get("requestedStart", "")))
        requested_end = date.fromisoformat(str(evidence.get("requestedEnd", "")))
        verified_at = datetime.fromisoformat(str(evidence.get("verifiedAt", "")).replace("Z", "+00:00"))
    except ValueError:
        return False
    if (
        requested_start > requested_end
        or (cutoff is not None and requested_end != cutoff)
        or verified_at.tzinfo is None
        or (
            "clippedOutOfRangeRows" in evidence
            and (
                not isinstance(evidence.get("clippedOutOfRangeRows"), int)
                or evidence.get("clippedOutOfRangeRows", -1) < 0
            )
        )
    ):
        return False
    row_hash = _canonical_hash(rows)
    if payload.get("seriesHash") != row_hash or evidence.get("sourceHash") != row_hash:
        return False
    previous: date | None = None
    for row in rows:
        if not isinstance(row, Mapping):
            return False
        try:
            item_date = date.fromisoformat(str(row.get("date", "")))
            price = float(row.get("close"))
        except (TypeError, ValueError):
            return False
        if not math.isfinite(price) or price <= 0 or (previous is not None and item_date <= previous):
            return False
        if item_date < requested_start or item_date > requested_end or (cutoff is not None and item_date > cutoff):
            return False
        previous = item_date
    return True


def _research_cache_path(state_dir: Path, cutoff: date, symbol: str) -> Path:
    safe_symbol = re.sub(r"[^A-Z0-9._-]", "_", symbol.upper())
    return state_dir / "research" / cutoff.isoformat() / f"{safe_symbol}.json"


def _load_research(state_dir: Path, cutoff: date, symbol: str) -> dict[str, Any] | None:
    payload = _read_json(_research_cache_path(state_dir, cutoff, symbol))
    if not _valid_sealed_cache(payload) or payload.get("dataCutoff") != cutoff.isoformat():
        return None
    result = payload.get("series")
    valid = _fx_history_is_valid(result, cutoff=cutoff) if symbol == "TWD=X" else _history_is_valid(result)
    return result if valid else None


def _save_research(state_dir: Path, cutoff: date, symbol: str, series: Mapping[str, Any]) -> None:
    valid = _fx_history_is_valid(series, cutoff=cutoff) if symbol == "TWD=X" else _history_is_valid(series)
    if not valid:
        return
    _write_json_atomic(
        _research_cache_path(state_dir, cutoff, symbol),
        _sealed_cache({"schemaVersion": 1, "dataCutoff": cutoff.isoformat(), "series": series}),
    )


def inventory_symbol_markets(inventory: Mapping[str, Any]) -> dict[str, str]:
    """Return unique positive holdings by market, excluding cash and collateral annotations."""
    result: dict[str, str] = {}
    for category, market in (("台股", "tw"), ("美股", "us"), ("基金", "other")):
        positions = inventory.get(category, {}) if isinstance(inventory, Mapping) else {}
        if not isinstance(positions, Mapping):
            continue
        for raw_symbol, raw_units in positions.items():
            symbol = str(raw_symbol or "").strip().upper()
            if not symbol or symbol == "HISTORY":
                continue
            try:
                units = float(raw_units)
            except (TypeError, ValueError):
                continue
            if units > 0:
                result[symbol] = market
    return result


def _automatic_document(payload: dict[str, Any], *, input_hash: str, validated_at: datetime, validated_by: str) -> dict[str, Any]:
    payload["autoValidation"] = {
        "algorithmVersion": AUTO_VALIDATION_ALGORITHM,
        "validationStatus": "PASS",
        "validatedAt": _iso_utc(validated_at),
        "validatedBy": validated_by,
        "inputHash": input_hash,
        "checks": list(AUTO_VALIDATION_CHECKS),
    }
    payload["autoValidation"]["payloadHash"] = _canonical_hash(payload)
    return payload


def _automatic_policy_current(path: Path, *, today: date, expected_cutoff: date, product: str) -> tuple[bool, dict[str, Any] | None]:
    payload = _read_json(path)
    if not payload or str(payload.get("approvalStatus", "")).upper() != "AUTO_VALIDATED":
        return False, payload
    if payload.get("dataCutoff") != expected_cutoff.isoformat():
        return False, payload
    if product == "beta":
        _, metadata, error = validate_active_policy_document(payload, as_of=today)
    else:
        metadata, error = validate_active_kelly_document(payload, as_of=today)
    return error is None and bool(metadata) and metadata.get("lifecycle") == "CURRENT", payload


def _candidate_cache(path: Path, *, cutoff: date) -> dict[str, Any] | None:
    payload = _read_json(path)
    if (
        not _valid_sealed_cache(payload)
        or payload.get("dataCutoff") != cutoff.isoformat()
        or payload.get("algorithmVersion") != AUTO_VALIDATION_ALGORITHM
    ):
        return None
    assets = payload.get("assets")
    if not isinstance(assets, Mapping):
        return None
    return payload


def _candidate_record_ready(record: Any) -> bool:
    if not isinstance(record, Mapping) or record.get("status") != "CANDIDATE":
        return False
    try:
        beta = float(record.get("beta"))
        observations = int(record.get("observations"))
    except (TypeError, ValueError):
        return False
    return math.isfinite(beta) and beta >= 0 and observations >= 104 and _valid_corporate_action_evidence(
        record.get("corporateActionEvidence"),
        window_start=_as_date(record.get("windowStart")),
        window_end=_as_date(record.get("windowEnd")),
    )


def _retry_due(record: Any, now: datetime) -> bool:
    if not isinstance(record, Mapping):
        return True
    reason_code = str(record.get("reasonCode", "")).upper()
    reason = str(record.get("reason", "")).lower()
    # A fixed point-in-time window cannot gain observations before the next
    # quarter. Keep structural failures visible without redownloading six
    # years of unchanged history on every settlement. Source/dependency
    # failures are retried on the next scheduled settlement.
    structural_codes = {
        "INSUFFICIENT_PAIRED_WEEK_OBSERVATIONS",
        "INSUFFICIENT_BETA_HISTORY",
        "OFFICIAL_ACTION_TYPE_UNSUPPORTED",
        "CORPORATE_ACTION_FACTOR_UNSUPPORTED",
    }
    if reason_code in structural_codes or "insufficient paired return observations" in reason:
        return False
    try:
        last_attempt = datetime.fromisoformat(str(record.get("attemptedAt", "")).replace("Z", "+00:00"))
    except ValueError:
        return True
    if last_attempt.tzinfo is None:
        return True
    # Keep retry timing tied to the settlement cadence, not a 24-hour delay.
    # The timestamp remains useful in private diagnostics and for deduping an
    # accidental rerun within the same workflow invocation.
    return now.astimezone(timezone.utc) - last_attempt.astimezone(timezone.utc) >= timedelta(minutes=30)


def _fetch_research_set(
    symbols: Mapping[str, str], *, start: date, cutoff: date, token: str | None, fetcher: Any,
    starts: Mapping[str, date] | None = None,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not symbols:
        return result
    pool = ThreadPoolExecutor(max_workers=min(4, len(symbols)))
    futures = {
        pool.submit(fetcher, symbol, market=market, start=(starts or {}).get(symbol, start), end=cutoff, token=token): (symbol, market)
        for symbol, market in symbols.items()
    }
    exhausted = False
    try:
        try:
            completed = as_completed(futures, timeout=RESEARCH_REQUEST_BUDGET_SECONDS)
            for future in completed:
                symbol, market = futures[future]
                try:
                    payload = future.result()
                except Exception as error:  # noqa: BLE001 - sanitized source boundary
                    payload = {
                        "symbol": symbol,
                        "market": market,
                        "currency": "TWD_PER_USD" if market == "fx" else "USD" if market == "us" else "TWD",
                        "rows": [],
                        "source": None,
                        "corporateActionStatus": "UNAVAILABLE",
                        "status": "UNAVAILABLE",
                        "reason": type(error).__name__,
                        "reasonCode": "SOURCE_UNAVAILABLE",
                    }
                normalized = dict(payload) if isinstance(payload, Mapping) else {}
                normalized.setdefault("symbol", symbol)
                normalized.setdefault("market", market)
                is_valid = _fx_history_is_valid(normalized) if symbol == "TWD=X" else _history_is_valid(normalized)
                normalized["status"] = "READY" if is_valid else "UNAVAILABLE"
                if not is_valid:
                    if not normalized.get("reason"):
                        normalized["reason"] = "research evidence failed validation"
                    if not normalized.get("reasonCode"):
                        normalized["reasonCode"] = "FX_SOURCE_UNAVAILABLE" if symbol == "TWD=X" else "RESEARCH_EVIDENCE_INVALID"
                result[symbol] = normalized
        except FuturesTimeoutError:
            exhausted = True
            for future, (symbol, market) in futures.items():
                if symbol in result:
                    continue
                future.cancel()
                result[symbol] = {
                    "symbol": symbol,
                    "market": market,
                    "currency": "TWD_PER_USD" if market == "fx" else "USD" if market == "us" else "TWD",
                    "rows": [],
                    "source": None,
                    "corporateActionStatus": "UNAVAILABLE",
                    "status": "UNAVAILABLE",
                    "reason": "quarterly research time budget exhausted",
                    "reasonCode": "RESEARCH_TIME_BUDGET_EXHAUSTED",
                }
    finally:
        pool.shutdown(wait=not exhausted, cancel_futures=True)
    return result


def ensure_quarterly_risk_policies(
    inventory: Mapping[str, Any],
    *,
    state_dir: str | Path = ".risk-policy-cache",
    candidate_dir: str | Path = ".private-build",
    today: date | None = None,
    now: datetime | None = None,
    fetcher: Any = fetch_research_series,
    fx_fallback_fetcher: Any | None = None,
) -> dict[str, Any]:
    """Prepare a verified Beta candidate and automatically activate independent Kelly policy."""
    current_date = today or datetime.now(timezone.utc).astimezone().date()
    now_utc = (now or _now_utc()).astimezone(timezone.utc)
    cutoff = previous_quarter_cutoff(current_date)
    completed_week_cutoff = last_completed_week_cutoff(cutoff)
    effective_quarter = _quarter_label(current_date)
    state = Path(state_dir)
    candidates = Path(candidate_dir)
    state.mkdir(parents=True, exist_ok=True)
    candidates.mkdir(parents=True, exist_ok=True)
    beta_active_path = state / "beta-policy-active.json"
    kelly_active_path = state / "kelly-policy-active.json"
    beta_candidate_cache_path = state / "beta-policy-candidate-cache.json"
    beta_candidate_output = candidates / "beta-policy-candidate.json"
    kelly_candidate_output = candidates / "kelly-quarterly-candidate.json"

    held_markets = inventory_symbol_markets(inventory)
    # FUND is a private accounting bucket, not a market ticker. Keep it
    # visible as unmodeled for coverage/Gate diagnostics; never query a stock
    # provider for a ticker literally named "FUND".
    unmodeled_symbols = sorted(symbol for symbol, market in held_markets.items() if market == "other")
    beta_symbols = {
        symbol: market for symbol, market in held_markets.items()
        if symbol not in FIXED_BETAS and market in {"tw", "us"}
    }
    beta_source_path = beta_active_path if beta_active_path.exists() else Path("config/beta-policy-active.json")
    kelly_source_path = kelly_active_path if kelly_active_path.exists() else Path("config/kelly-policy-active.json")
    beta_is_current, beta_doc = _automatic_policy_current(
        beta_source_path, today=current_date, expected_cutoff=cutoff, product="beta",
    )
    kelly_is_current, kelly_doc = _automatic_policy_current(
        kelly_source_path, today=current_date, expected_cutoff=cutoff, product="kelly",
    )
    cached_candidate = _candidate_cache(beta_candidate_cache_path, cutoff=cutoff)
    same_quarter_base: dict[str, Any] = {}
    if beta_is_current and isinstance(beta_doc, Mapping):
        same_quarter_base.update(beta_doc.get("assets", {}))
    if cached_candidate:
        for symbol, record in cached_candidate.get("assets", {}).items():
            if symbol not in same_quarter_base or not _candidate_record_ready(same_quarter_base[symbol]):
                same_quarter_base[symbol] = record

    retry_symbols = {
        symbol: market for symbol, market in beta_symbols.items()
        if not _candidate_record_ready(same_quarter_base.get(symbol))
        and _retry_due(same_quarter_base.get(symbol), now_utc)
    }
    beta_refresh_needed = not beta_is_current or bool(retry_symbols)
    kelly_refresh_needed = not kelly_is_current
    benchmark_needed = not beta_is_current or bool(retry_symbols) or kelly_refresh_needed
    histories: dict[str, dict[str, Any]] = {}
    benchmark = _load_research(state, cutoff, "006208")
    fx_history = _load_research(state, cutoff, "TWD=X")
    fx_source_selection: dict[str, Any] | None = (
        {
            "source": fx_history.get("source"),
            "seriesHash": fx_history.get("seriesHash"),
            "fallbackFrom": None,
            "fallbackReason": None,
        }
        if isinstance(fx_history, Mapping) else None
    )

    fetch_symbols: dict[str, str] = {}
    if benchmark_needed and benchmark is None:
        fetch_symbols["006208"] = "tw"
    if beta_refresh_needed and retry_symbols:
        fetch_symbols.update(retry_symbols)
    if fx_history is None and any(market == "us" for market in retry_symbols.values()):
        fetch_symbols["TWD=X"] = "fx"
    token = os.getenv("FINMIND_TOKEN", "").strip() or None
    if fetch_symbols:
        beta_start = cutoff - timedelta(days=3 * 365 + 14)
        kelly_start = cutoff - timedelta(days=5 * 365 + 14)
        starts = {
            symbol: kelly_start if symbol == "006208" and kelly_refresh_needed else beta_start
            for symbol in fetch_symbols
        }
        start = min(starts.values())
        with official_action_cache_scope():
            fetched = _fetch_research_set(
                fetch_symbols, start=start, cutoff=cutoff, token=token, fetcher=fetcher, starts=starts,
        )
        for symbol, payload in fetched.items():
            if symbol != "TWD=X" and payload.get("status") == "READY":
                _save_research(state, cutoff, symbol, payload)
            histories[symbol] = payload
        benchmark = benchmark or histories.get("006208")
        fx_history = fx_history or histories.get("TWD=X")
        if fx_source_selection is None and isinstance(fx_history, Mapping):
            fx_source_selection = {
                "source": fx_history.get("source"),
                "seriesHash": fx_history.get("seriesHash"),
                "fallbackFrom": None,
                "fallbackReason": None,
            }

    if beta_refresh_needed:
        fresh_asset_symbols = set(retry_symbols)
        fresh_histories = {symbol: histories[symbol] for symbol in fresh_asset_symbols if symbol in histories}
        benchmark_rows = benchmark.get("rows", []) if isinstance(benchmark, Mapping) else []
        fx_rows = fx_history.get("rows", []) if isinstance(fx_history, Mapping) and _fx_history_is_valid(fx_history, cutoff=cutoff) else []
        estimates = estimate_beta_policy(
            fresh_histories,
            benchmark_rows,
            fx_rows=fx_rows,
            cutoff=cutoff,
        ) if fresh_asset_symbols else {"assets": {}}
        fx_gap_symbols = sorted(
            symbol for symbol in fresh_asset_symbols
            if retry_symbols.get(symbol) == "us"
            and isinstance(estimates.get("assets", {}).get(symbol), Mapping)
            and estimates["assets"][symbol].get("reason")
            == "verified USD/TWD observations do not cover paired weeks inside the three-year estimation window"
        )
        current_fx_provider = (
            (fx_history.get("fxEvidence") or {}).get("provider")
            if isinstance(fx_history, Mapping) and isinstance(fx_history.get("fxEvidence"), Mapping)
            else None
        )
        fallback_fetcher = fx_fallback_fetcher
        if fallback_fetcher is None and fetcher is fetch_research_series:
            fallback_fetcher = fetch_cbc_usd_twd_series
        # A syntactically valid Yahoo FX response can still miss required
        # paired weeks. In that case, replace the entire FX history with CBC
        # and re-estimate every unresolved asset as one coherent source set;
        # never splice daily observations from both providers.
        if fx_gap_symbols and current_fx_provider == "Yahoo Chart API" and fallback_fetcher:
            old_fx_hash = str(fx_source_selection.get("seriesHash") or "")
            try:
                cbc_history = fallback_fetcher(
                    start=cutoff - timedelta(days=3 * 365 + 14), end=cutoff,
                )
            except Exception as error:  # noqa: BLE001 - preserve the primary diagnosis and fallback evidence
                fx_source_selection["fallbackReason"] = f"CBC fallback failed: {type(error).__name__}"
            else:
                if _fx_history_is_valid(cbc_history, cutoff=cutoff):
                    cbc_estimates = estimate_beta_policy(
                        fresh_histories,
                        benchmark_rows,
                        fx_rows=cbc_history.get("rows", []),
                        cutoff=cutoff,
                    ) if fresh_asset_symbols else {"assets": {}}
                    fx_history = cbc_history
                    histories["TWD=X"] = cbc_history
                    estimates = cbc_estimates
                    fx_source_selection = {
                        "source": cbc_history.get("source"),
                        "seriesHash": cbc_history.get("seriesHash"),
                        "fallbackFrom": old_fx_hash or None,
                        "fallbackReason": "Yahoo FX did not cover required completed paired weeks",
                    }
                else:
                    fx_source_selection["fallbackReason"] = "CBC fallback failed FX contract validation"
        unresolved_fx_gaps = [
            symbol for symbol in fresh_asset_symbols
            if retry_symbols.get(symbol) == "us"
            and isinstance(estimates.get("assets", {}).get(symbol), Mapping)
            and estimates["assets"][symbol].get("reason")
            == "verified USD/TWD observations do not cover paired weeks inside the three-year estimation window"
        ]
        if fx_history is not None and not unresolved_fx_gaps and _fx_history_is_valid(fx_history, cutoff=cutoff):
            # Persist FX only after this quarter's required paired weeks prove
            # complete. A merely well-formed partial response must never
            # replace a previously verified complete provider cache.
            _save_research(state, cutoff, "TWD=X", fx_history)
        candidate_assets = deepcopy(same_quarter_base)
        for symbol in fresh_asset_symbols:
            record = deepcopy(estimates.get("assets", {}).get(symbol) or {
                "status": "INSUFFICIENT_EVIDENCE", "reason": "research series unavailable", "observations": 0,
            })
            record["attemptedAt"] = _iso_utc(now_utc)
            candidate_assets[symbol] = record
        for symbol in beta_symbols:
            if symbol not in candidate_assets:
                candidate_assets[symbol] = {
                    "status": "INSUFFICIENT_EVIDENCE",
                    "reason": "no verified estimate for current holding",
                    "observations": 0,
                    "attemptedAt": _iso_utc(now_utc),
                }
        evidence_hashes = {
            symbol: (record.get("corporateActionEvidence") or {}).get("seriesHash")
            for symbol, record in sorted(candidate_assets.items())
            if isinstance(record, Mapping)
        }
        input_hash = _canonical_hash({
            "dataCutoff": cutoff.isoformat(),
            "completedWeekCutoff": completed_week_cutoff.isoformat(),
            "benchmarkSeriesHash": (benchmark or {}).get("seriesHash") if isinstance(benchmark, Mapping) else None,
            "fxSeriesHash": (fx_history or {}).get("seriesHash") if isinstance(fx_history, Mapping) else None,
            "assetSeriesHashes": evidence_hashes,
            "holdings": sorted(beta_symbols),
        })
        candidate_payload = {
            "schemaVersion": 1,
            "status": (
                "CANDIDATE"
                if all(_candidate_record_ready(candidate_assets.get(symbol)) for symbol in beta_symbols)
                and (isinstance(benchmark, Mapping) and _history_is_valid(benchmark))
                else "PARTIAL_CANDIDATE_BLOCKED"
            ),
            "approvalStatus": "AUTOMATIC_CANDIDATE",
            "policyVersion": f"candidate-{effective_quarter}-{input_hash[:12]}",
            "algorithmVersion": AUTO_VALIDATION_ALGORITHM,
            "benchmark": "006208.TW/TWD",
            "dataCutoff": cutoff.isoformat(),
            "completedWeekCutoff": completed_week_cutoff.isoformat(),
            "effectiveFromQuarter": effective_quarter,
            "referenceThroughQuarter": _quarter_label(date(current_date.year + (1 if current_date.month >= 10 else 0), ((current_date.month - 1 + 3) % 12) + 1, 1)),
            "assets": candidate_assets,
            "fxSourceSelection": fx_source_selection,
            "unmodeledSymbols": unmodeled_symbols,
            "contentHash": _canonical_hash(candidate_assets),
            "inputHash": input_hash,
            "updatedAt": _iso_utc(now_utc),
        }
        _write_json_atomic(beta_candidate_output, candidate_payload)
        _write_json_atomic(beta_candidate_cache_path, _sealed_cache(candidate_payload))
        _write_json_atomic(state / "beta-policy-candidate.json", _sealed_cache(candidate_payload))
        immutable_candidate = candidates / f"beta-policy-candidate-{effective_quarter}-{input_hash[:16]}.json"
        if not immutable_candidate.exists():
            _write_json_atomic(immutable_candidate, candidate_payload)

    kelly_summary: dict[str, Any] = {"status": "REUSED" if kelly_is_current else "NOT_READY"}
    if kelly_refresh_needed:
        benchmark = benchmark or _load_research(state, cutoff, "006208")
        benchmark_evidence = (benchmark or {}).get("corporateActionEvidence") if isinstance(benchmark, Mapping) else None
        benchmark_rows = (benchmark or {}).get("rows", []) if isinstance(benchmark, Mapping) else []
        weeklies = weekly_research_series(
            benchmark_rows,
            price_key="totalReturnIndex",
            cutoff=completed_week_cutoff,
        )
        five_year_start = completed_week_cutoff - timedelta(days=5 * 365)
        prices = [
            row for row in weeklies
            if (_as_date(row.get("date")) or date.min) >= five_year_start - timedelta(days=7)
        ]
        kelly_result = build_quarterly_kelly_candidate(prices, data_cutoff=cutoff.isoformat(), min_observations=0)
        window_start = _as_date(prices[0].get("date")) if prices else None
        window_end = _as_date(prices[-1].get("date")) if prices else None
        kelly_window_complete = bool(
            window_start is not None
            and window_end is not None
            and window_start <= five_year_start + timedelta(days=7)
            and completed_week_cutoff - window_end <= timedelta(days=10)
        )
        kelly_evidence_ok = _valid_corporate_action_evidence(
            benchmark_evidence,
            window_start=window_start,
            window_end=window_end,
        )
        kelly_ready = (
            kelly_result.get("status") == "CANDIDATE"
            and kelly_evidence_ok
            and int(kelly_result.get("volatilityObservations", 0)) >= 104
            and kelly_window_complete
            and all(_as_date(row.get("date")) is not None and _as_date(row.get("date")) <= completed_week_cutoff for row in prices)
        )
        kelly_reason = None
        if not kelly_ready:
            if kelly_result.get("status") != "CANDIDATE":
                kelly_reason = kelly_result.get("reason") or "completed five-year sample unavailable"
            elif not kelly_window_complete:
                kelly_reason = "benchmark completed-week window is stale or does not cover five years"
            elif not kelly_evidence_ok:
                kelly_reason = "benchmark corporate-action evidence unavailable"
            else:
                kelly_reason = "completed five-year weekly sample or 104 return observations unavailable"
        input_hash = _canonical_hash({
            "dataCutoff": cutoff.isoformat(),
            "completedWeekCutoff": completed_week_cutoff.isoformat(),
            "benchmarkSeriesHash": (benchmark or {}).get("seriesHash") if isinstance(benchmark, Mapping) else None,
            "corporateActionEventsHash": (benchmark_evidence or {}).get("eventsHash") if isinstance(benchmark_evidence, Mapping) else None,
            "weeklyTotalReturnPrices": prices,
        })
        kelly_candidate = {
            "schemaVersion": 1,
            "status": "CANDIDATE" if kelly_ready else "INSUFFICIENT_EVIDENCE",
            "reason": kelly_reason,
            "mu": kelly_result.get("mu") if kelly_ready else None,
            "sigma": kelly_result.get("sigma") if kelly_ready else None,
            "halfKellyLimit": kelly_result.get("halfKellyLimit") if kelly_ready else None,
            "dataCutoff": cutoff.isoformat(),
            "windowStart": window_start.isoformat() if window_start else None,
            "windowEnd": window_end.isoformat() if window_end else None,
            "observations": kelly_result.get("observations", len(prices)),
            "source": (benchmark or {}).get("source") if isinstance(benchmark, Mapping) else None,
            "corporateActionEvidence": benchmark_evidence,
            "inputHash": input_hash,
            "effectiveFromQuarter": effective_quarter,
            "referenceThroughQuarter": candidate_payload.get("referenceThroughQuarter") if beta_refresh_needed else _quarter_label(date(current_date.year + (1 if current_date.month >= 10 else 0), ((current_date.month - 1 + 3) % 12) + 1, 1)),
            "updatedAt": _iso_utc(now_utc),
        }
        _write_json_atomic(kelly_candidate_output, kelly_candidate)
        immutable_kelly = candidates / f"kelly-quarterly-candidate-{effective_quarter}-{input_hash[:16]}.json"
        if not immutable_kelly.exists():
            _write_json_atomic(immutable_kelly, kelly_candidate)
        if kelly_ready:
            active_kelly = {
                **kelly_candidate,
                "status": "ACTIVE",
                "approvalStatus": "AUTO_VALIDATED",
                "activeVersion": f"{effective_quarter}-{input_hash[:12]}",
                "policyVersion": f"{effective_quarter}-{input_hash[:12]}",
                "contentHash": _canonical_hash({key: value for key, value in kelly_candidate.items() if key not in {"updatedAt", "status", "reason"}}),
            }
            active_kelly = _automatic_document(
                active_kelly,
                input_hash=input_hash,
                validated_at=now_utc,
                validated_by="trusted-main-workflow",
            )
            _, error = validate_active_kelly_document(active_kelly, as_of=current_date)
            if error is None:
                _write_json_atomic(kelly_active_path, active_kelly)
                kelly_summary = {"status": "AUTO_VALIDATED", "policyVersion": active_kelly["activeVersion"], "dataCutoff": cutoff.isoformat()}
            else:
                kelly_summary = {"status": "REJECTED", "reasonCode": error}
        else:
            kelly_summary = {"status": "INSUFFICIENT_EVIDENCE", "reasonCode": kelly_candidate["reason"]}

    summary = {
        "schemaVersion": 1,
        "algorithmVersion": AUTO_VALIDATION_ALGORITHM,
        "effectiveQuarter": effective_quarter,
        "dataCutoff": cutoff.isoformat(),
        "completedWeekCutoff": completed_week_cutoff.isoformat(),
        "beta": {
            "status": (
                "CANDIDATE_READY_FOR_LIVE_COVERAGE"
                if beta_refresh_needed and beta_candidate_output.exists()
                and _read_json(beta_candidate_output)
                and _read_json(beta_candidate_output).get("status") == "CANDIDATE"
                else "PARTIAL_CANDIDATE_BLOCKED"
                if beta_refresh_needed and beta_candidate_output.exists()
                else "REUSED" if beta_is_current else "WAITING_FOR_PORTFOLIO_QUALIFICATION"
            ),
            "symbolsRequested": sorted(beta_symbols),
            "fxSourceSelection": fx_source_selection,
            "unmodeledSymbols": unmodeled_symbols,
            "symbolsUnresolved": sorted(
                symbol for symbol in beta_symbols
                if not _candidate_record_ready(((_read_json(beta_candidate_output) or {}).get("assets") or {}).get(symbol))
            ) if beta_candidate_output.exists() else sorted(beta_symbols),
            "failedSymbols": [
                {
                    "symbol": symbol,
                    "status": record.get("status"),
                    "reasonCode": record.get("reasonCode") or record.get("reason") or "BETA_EVIDENCE_UNAVAILABLE",
                    "observations": record.get("observations", 0),
                    "attemptedAt": record.get("attemptedAt"),
                }
                for symbol, record in sorted(((_read_json(beta_candidate_output) or {}).get("assets") or {}).items())
                if isinstance(record, Mapping) and not _candidate_record_ready(record)
            ] if beta_candidate_output.exists() else [],
            "activationReady": bool(
                beta_is_current or (
                    beta_candidate_output.exists()
                    and (_read_json(beta_candidate_output) or {}).get("status") == "CANDIDATE"
                    and not unmodeled_symbols
                    and not any(
                        not _candidate_record_ready(((_read_json(beta_candidate_output) or {}).get("assets") or {}).get(symbol))
                        for symbol in beta_symbols
                    )
                )
            ),
            "retryPolicy": {
                "sourceFailure": "NEXT_SCHEDULED_SETTLEMENT",
                "structuralInsufficiency": "NEXT_QUARTER_OR_DATA_CHANGE",
            },
            "candidatePath": str(beta_candidate_output),
        },
        "kelly": kelly_summary,
        "sourceFailures": sorted(
            symbol for symbol, value in histories.items()
            if value.get("status") != "READY"
        ),
    }
    _write_json_atomic(candidates / "quarterly-risk-policy-summary.json", summary)
    return summary


def promote_beta_candidate_if_qualified(
    asset_values_twd: Mapping[str, float],
    total_asset: float,
    total_debt: float,
    *,
    market_by_symbol: Mapping[str, str],
    state_dir: str | Path = ".risk-policy-cache",
    today: date | None = None,
    now: datetime | None = None,
    validated_by: str = "trusted-main-workflow",
) -> dict[str, Any]:
    """Promote the quarterly Beta only after this run's actual NAV coverage passes."""
    current_date = today or datetime.now(timezone.utc).astimezone().date()
    now_utc = (now or _now_utc()).astimezone(timezone.utc)
    state = Path(state_dir)
    candidate_path = state / "beta-policy-candidate.json"
    active_path = state / "beta-policy-active.json"
    candidate = _read_json(candidate_path)
    if (
        not isinstance(candidate, Mapping)
        or not _valid_sealed_cache(candidate)
        or candidate.get("algorithmVersion") != AUTO_VALIDATION_ALGORITHM
        or candidate.get("dataCutoff") != previous_quarter_cutoff(current_date).isoformat()
    ):
        return {"status": "NO_CURRENT_CANDIDATE", "reasonCode": "quarter_candidate_missing_or_algorithm_mismatch"}
    input_hash = str(candidate.get("inputHash", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", input_hash):
        return {"status": "REJECTED", "reasonCode": "candidate_input_hash_invalid"}
    assets = candidate.get("assets")
    if not isinstance(assets, Mapping) or candidate.get("contentHash") != _canonical_hash(assets):
        return {"status": "REJECTED", "reasonCode": "candidate_content_hash_invalid"}
    existing = _read_json(active_path)
    if (
        isinstance(existing, Mapping)
        and existing.get("dataCutoff") == candidate.get("dataCutoff")
        and existing.get("inputHash") == input_hash
        and str(existing.get("approvalStatus", "")).upper() == "AUTO_VALIDATED"
    ):
        existing_betas, _, existing_error = validate_active_policy_document(existing, as_of=current_date)
        if existing_error is None:
            existing_result = calculate_nav_beta(
                asset_values_twd,
                total_asset,
                total_debt,
                {**FIXED_BETAS, **existing_betas},
                market_by_symbol=market_by_symbol,
            )
            if existing_result.get("status") == "READY":
                return {
                    "status": "ALREADY_ACTIVE",
                    "policyVersion": existing.get("policyVersion"),
                    "dataCutoff": existing.get("dataCutoff"),
                    "coveragePct": existing_result.get("coveragePct"),
                }
    active = {
        key: deepcopy(value) for key, value in candidate.items()
        if key not in {"cacheHash", "updatedAt", "attemptedAt"}
    }
    active.update({
        "status": "ACTIVE",
        "approvalStatus": "AUTO_VALIDATED",
        "policyVersion": str(candidate.get("policyVersion") or f"{_quarter_label(current_date)}-{input_hash[:12]}"),
        "activeVersion": f"{_quarter_label(current_date)}-{input_hash[:12]}",
    })
    active = _automatic_document(
        active,
        input_hash=input_hash,
        validated_at=now_utc,
        validated_by=validated_by,
    )
    beta_map, _, error = validate_active_policy_document(active, as_of=current_date)
    if error:
        return {"status": "REJECTED", "reasonCode": error}
    result = calculate_nav_beta(
        asset_values_twd,
        total_asset,
        total_debt,
        {**FIXED_BETAS, **beta_map},
        market_by_symbol=market_by_symbol,
    )
    if result.get("status") != "READY":
        return {
            "status": "WAITING_FOR_PORTFOLIO_QUALIFICATION",
            "reasonCode": result.get("quality") or "beta_coverage_unavailable",
            "coveragePct": result.get("coveragePct"),
            "missingSymbols": sorted(item.get("symbol", "") for item in result.get("missing", [])),
        }
    _write_json_atomic(active_path, active)
    return {
        "status": "AUTO_ACTIVATED",
        "policyVersion": active["policyVersion"],
        "dataCutoff": active["dataCutoff"],
        "coveragePct": result.get("coveragePct"),
    }


def active_policy_path(state_dir: str | Path, filename: str, fallback: str) -> str:
    path = Path(state_dir) / filename
    return str(path) if path.exists() else fallback
