"""Build private quarterly Beta and Kelly research candidates.

Production activation is performed inside the settlement pipeline after the
actual holdings and NAV coverage pass. This standalone command never promotes
a candidate or writes portfolio data.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from beta_policy import (
    FIXED_BETAS,
    estimate_beta_policy,
    fetch_research_series,
    last_completed_week_cutoff,
    previous_quarter_cutoff,
    weekly_research_series,
)
from risk import build_quarterly_kelly_candidate


SYMBOL_MARKETS = {
    "00403A": "tw", "00886": "tw", "00895": "tw", "00878": "tw",
    "3455": "tw", "8033": "tw", "2330": "tw", "3665": "tw",
    "QQQM": "us", "NVDA": "us", "SPYG": "us", "TSM": "us",
    "VOO": "us", "VTI": "us", "TSLA": "us", "AAPL": "us", "QQQ": "us",
    # FUND is deliberately retained as an explicit unavailable position until
    # the ledger supplies an identifiable, researchable fund symbol.
    "FUND": "other",
}


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    output = Path(os.getenv("RISK_CANDIDATE_OUTPUT_DIR", ".private-build"))
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    cutoff = previous_quarter_cutoff(today)
    start = cutoff - timedelta(days=6 * 365)
    completed_week_cutoff = last_completed_week_cutoff(cutoff)

    requested = {"006208": "tw", **SYMBOL_MARKETS}
    requested["TWD=X"] = "us"
    histories: dict[str, dict] = {}
    finmind_token = os.getenv("FINMIND_TOKEN", "").strip() or None
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(fetch_research_series, symbol, market=market, start=start, end=cutoff, token=finmind_token): (symbol, market)
            for symbol, market in requested.items()
        }
        for future in as_completed(futures):
            symbol, market = futures[future]
            try:
                result = future.result()
            except Exception as error:  # noqa: BLE001 - candidate diagnostics
                result = {"symbol": symbol, "market": market, "currency": "TWD" if market == "tw" else "USD", "rows": [], "status": "UNAVAILABLE", "reason": type(error).__name__, "corporateActionStatus": "UNAVAILABLE", "source": None}
            if symbol == "006208":
                histories["006208"] = result
            elif symbol == "TWD=X":
                histories["TWD=X"] = result
            else:
                histories[symbol] = result

    benchmark = histories.get("006208", {})
    fx = histories.get("TWD=X", {})
    asset_histories = {symbol: record for symbol, record in histories.items() if symbol not in {"006208", "TWD=X"}}
    candidate = estimate_beta_policy(
        asset_histories,
        benchmark.get("rows", []),
        fx_rows=fx.get("rows", []),
        cutoff=cutoff,
    )
    candidate["source"] = "Yahoo Chart raw OHLC + research-price contract v1"
    candidate["fixedPolicy"] = FIXED_BETAS
    candidate_inputs = {
        "dataCutoff": cutoff.isoformat(),
        "completedWeekCutoff": completed_week_cutoff.isoformat(),
        "benchmark": benchmark,
        "fx": fx,
        "assets": {symbol: histories[symbol] for symbol in sorted(asset_histories)},
    }
    candidate_input_hash = hashlib.sha256(
        json.dumps(candidate_inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    candidate["inputHash"] = candidate_input_hash
    candidate["candidateId"] = f"{candidate['effectiveFromQuarter']}-{candidate_input_hash[:16]}"
    _write(output / "beta-policy-candidate.json", candidate)
    immutable_beta_path = output / f"beta-policy-candidate-{candidate['candidateId']}.json"
    if not immutable_beta_path.exists():
        _write(immutable_beta_path, candidate)

    weekly_benchmark = weekly_research_series(
        benchmark.get("rows", []),
        price_key="totalReturnIndex",
        cutoff=completed_week_cutoff,
    )
    # μ is the point-in-time five-year CAGR.  The fetch window is longer so
    # the latest 261 weekly points (five years including endpoints) are the
    # only observations supplied to the quarterly Kelly estimator.
    prices = weekly_benchmark[-261:]
    kelly = build_quarterly_kelly_candidate(prices, data_cutoff=cutoff.isoformat())
    canonical = json.dumps({"prices": prices, "source": benchmark.get("source"),
                            "corporateActionStatus": benchmark.get("corporateActionStatus"),
                            "dataCutoff": cutoff.isoformat()},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    kelly_content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    kelly_candidate = {
        "schemaVersion": 1,
        "status": kelly.get("status"),
        "reason": kelly.get("reason"),
        "mu": kelly.get("mu"),
        "sigma": kelly.get("sigma"),
        "halfKellyLimit": kelly.get("halfKellyLimit"),
        "dataCutoff": kelly.get("dataCutoff", cutoff.isoformat()),
        "source": benchmark.get("source"),
        "contentHash": kelly_content_hash if prices else None,
        "candidateId": f"{candidate['effectiveFromQuarter']}-{kelly_content_hash[:16]}",
        "inputHash": kelly_content_hash if prices else None,
        "effectiveFromQuarter": candidate["effectiveFromQuarter"],
        "referenceThroughQuarter": candidate["referenceThroughQuarter"],
        "corporateActionStatus": benchmark.get("corporateActionStatus", "UNAVAILABLE"),
        "approvalStatus": "AUTOMATIC_CANDIDATE" if kelly.get("status") == "CANDIDATE" else "NOT_READY",
    }
    _write(output / "kelly-quarterly-candidate.json", kelly_candidate)
    immutable_kelly_path = output / f"kelly-quarterly-candidate-{kelly_candidate['candidateId']}.json"
    if not immutable_kelly_path.exists():
        _write(immutable_kelly_path, kelly_candidate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
