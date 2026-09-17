"""Build private quarterly Beta and Kelly review artifacts.

This command is intentionally review-only: it never changes the active
policy, portfolio ledger, Telegram markers, or public Pages files.
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
    start = cutoff - timedelta(days=8 * 365)

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
    _write(output / "beta-policy-candidate.json", candidate)

    weekly_benchmark = weekly_research_series(
        benchmark.get("rows", []),
        price_key="totalReturnIndex",
        cutoff=cutoff,
    )
    # μ is the point-in-time five-year CAGR.  The fetch window is longer so
    # the latest 261 weekly points (five years including endpoints) are the
    # only observations supplied to the quarterly Kelly estimator.
    prices = weekly_benchmark[-261:]
    kelly = build_quarterly_kelly_candidate(prices, data_cutoff=cutoff.isoformat())
    canonical = json.dumps(prices, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _write(output / "kelly-quarterly-candidate.json", {
        "schemaVersion": 1,
        "status": kelly.get("status"),
        "reason": kelly.get("reason"),
        "mu": kelly.get("mu"),
        "sigma": kelly.get("sigma"),
        "halfKellyLimit": kelly.get("halfKellyLimit"),
        "dataCutoff": kelly.get("dataCutoff", cutoff.isoformat()),
        "source": benchmark.get("source"),
        "contentHash": hashlib.sha256(canonical.encode("utf-8")).hexdigest() if prices else None,
        "corporateActionStatus": benchmark.get("corporateActionStatus", "UNAVAILABLE"),
        "approvalStatus": "PENDING" if kelly.get("status") == "CANDIDATE" else "NOT_READY",
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
