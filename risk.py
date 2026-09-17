"""Pure, testable portfolio-risk calculations used by the dashboard.

The NAV-Beta helpers in this module deliberately accept already validated
market values.  Fetching prices, corporate actions and credentials belongs to
the data layer; keeping the arithmetic here makes the contract replayable in
unit tests and prevents a pledged holding from being counted twice.
"""

from __future__ import annotations

import math
from typing import Mapping

HALF_KELLY_LIMIT = 0.08 / (2 * (0.18 ** 2))
BETA_SCHEMA_VERSION = 1
BETA_COVERAGE_MIN_PERCENT = 95.0
BETA_MATERIAL_NAV_PERCENT = 1.0
FIXED_BETA_POLICY = {"006208": 1.0, "00685L": 2.0}


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def calculate_nav_beta(
    asset_values_twd: Mapping[str, float],
    total_asset: float,
    total_debt: float,
    beta_by_symbol: Mapping[str, float | None],
    *,
    market_by_symbol: Mapping[str, str] | None = None,
    coverage_min_percent: float = BETA_COVERAGE_MIN_PERCENT,
    material_nav_percent: float = BETA_MATERIAL_NAV_PERCENT,
) -> dict:
    """Calculate canonical NAV Beta from one de-duplicated asset ledger.

    ``asset_values_twd`` must contain each economic holding once.  Collateral
    annotations are intentionally not accepted as a second value map.  A
    missing beta never becomes zero: material or insufficiently covered
    holdings make the formal result unavailable.
    """
    total_asset_value = _finite_number(total_asset)
    debt_value = _finite_number(total_debt)
    if total_asset_value is None or debt_value is None or total_asset_value < 0 or debt_value < 0:
        return {"status": "UNAVAILABLE", "quality": "invalid_balance", "reason": "invalid asset or debt total"}
    nav = total_asset_value - debt_value
    if nav <= 0:
        return {"status": "UNAVAILABLE", "quality": "non_positive_nav", "reason": "NAV must be positive", "nav": nav}

    values: dict[str, float] = {}
    for symbol, raw_value in (asset_values_twd or {}).items():
        value = _finite_number(raw_value)
        if value is None or value < 0:
            return {"status": "UNAVAILABLE", "quality": "invalid_asset_value", "reason": f"invalid value for {symbol}"}
        if value > 0:
            values[str(symbol)] = value

    risk_asset_value = sum(value for symbol, value in values.items() if str(symbol).upper() not in {"CASH", "CASH_TWD", "CASH_USD", "現金", "現金_TWD", "現金_USD"})
    covered_value = 0.0
    exposure = 0.0
    missing: list[dict] = []
    contributions = {"tw": 0.0, "us": 0.0, "other": 0.0}
    positions: list[dict] = []
    for symbol, value in values.items():
        beta = _finite_number((beta_by_symbol or {}).get(symbol))
        is_cash = str(symbol).upper() in {"CASH", "CASH_TWD", "CASH_USD", "現金", "現金_TWD", "現金_USD"}
        if is_cash:
            beta = 0.0
        if beta is None or beta < 0:
            nav_percent = value / nav * 100 if nav else math.inf
            missing.append({"symbol": symbol, "valueTwd": round(value, 2), "material": nav_percent >= material_nav_percent})
            positions.append({"symbol": symbol, "valueTwd": round(value, 2), "beta": None, "covered": False})
            continue
        covered_value += value if not is_cash else 0.0
        position_exposure = value * beta
        exposure += position_exposure
        market = str((market_by_symbol or {}).get(symbol, "other")).lower()
        bucket = "tw" if market in {"tw", "taiwan", "twd"} else "us" if market in {"us", "usa", "usd"} else "other"
        contributions[bucket] += position_exposure / nav
        positions.append({"symbol": symbol, "valueTwd": round(value, 2), "beta": beta, "covered": True, "market": bucket})

    coverage = 100.0 if risk_asset_value <= 0 else covered_value / risk_asset_value * 100
    material_missing = [item for item in missing if item["material"]]
    formal_ready = not material_missing and coverage >= coverage_min_percent
    if not formal_ready:
        return {
            "status": "UNAVAILABLE",
            "quality": "insufficient_beta_coverage",
            "reason": "material holding beta unavailable" if material_missing else "beta coverage below policy minimum",
            "nav": round(nav, 2),
            "totalAsset": round(total_asset_value, 2),
            "totalDebt": round(debt_value, 2),
            "coveragePct": round(coverage, 2),
            "missing": missing,
            "positions": positions,
        }
    asset_beta = exposure / total_asset_value if total_asset_value else 0.0
    nav_beta = exposure / nav
    return {
        "status": "READY",
        "quality": "policy_complete",
        "schemaVersion": BETA_SCHEMA_VERSION,
        "nav": round(nav, 2),
        "totalAsset": round(total_asset_value, 2),
        "totalDebt": round(debt_value, 2),
        "betaExposureTwd": round(exposure, 2),
        "assetBeta": round(asset_beta, 8),
        "navBeta": round(nav_beta, 8),
        "grossLeverage": round(total_asset_value / nav, 8),
        "debtToNav": round(debt_value / nav, 8),
        "coveragePct": round(coverage, 2),
        "missing": missing,
        "contributions": {key: round(value, 8) for key, value in contributions.items()},
        "positions": positions,
    }


def estimate_beta_from_returns(asset_returns, benchmark_returns, *, min_observations: int = 104):
    """Estimate beta using paired completed returns without provider policy."""
    pairs = []
    for asset, benchmark in zip(asset_returns or [], benchmark_returns or []):
        a, b = _finite_number(asset), _finite_number(benchmark)
        if a is not None and b is not None:
            pairs.append((a, b))
    if len(pairs) < min_observations:
        return {"status": "UNAVAILABLE", "reason": "insufficient paired observations", "observations": len(pairs)}
    mean_a = sum(a for a, _ in pairs) / len(pairs)
    mean_b = sum(b for _, b in pairs) / len(pairs)
    variance = sum((b - mean_b) ** 2 for _, b in pairs)
    if variance <= 0:
        return {"status": "UNAVAILABLE", "reason": "benchmark variance is zero", "observations": len(pairs)}
    covariance = sum((a - mean_a) * (b - mean_b) for a, b in pairs)
    beta = covariance / variance
    if not math.isfinite(beta) or beta < 0:
        return {"status": "UNAVAILABLE", "reason": "estimated beta is invalid", "observations": len(pairs)}
    return {"status": "READY", "beta": beta, "observations": len(pairs), "method": "paired_weekly_simple_returns"}


def quarterly_half_kelly(mu, sigma, *, mu_cap=0.08, sigma_floor=0.18):
    """Return a conservative, point-in-time half-Kelly parameter candidate."""
    expected = _finite_number(mu)
    volatility = _finite_number(sigma)
    if expected is None or volatility is None or expected <= 0 or volatility <= 0:
        return {"status": "UNAVAILABLE", "reason": "mu and sigma must be positive"}
    expected = min(expected, mu_cap)
    volatility = max(volatility, sigma_floor)
    limit = expected / (2 * volatility ** 2)
    return {"status": "CANDIDATE", "mu": expected, "sigma": volatility, "halfKellyLimit": limit}


def build_quarterly_kelly_candidate(total_return_prices, *, data_cutoff=None, min_observations=260):
    """Build a point-in-time quarterly candidate from completed weekly prices.

    The caller supplies a split/dividend-normalised research series; this
    function never reads a provider's ex-post adjusted-close field.  Dates
    after ``data_cutoff`` are excluded before either statistic is computed.
    """
    rows = []
    for row in total_return_prices or []:
        if isinstance(row, Mapping):
            raw_date, raw_price = row.get("date"), row.get("close", row.get("price"))
        else:
            try:
                raw_date, raw_price = row
            except (TypeError, ValueError):
                continue
        if data_cutoff is not None and str(raw_date) > str(data_cutoff):
            continue
        price = _finite_number(raw_price)
        if price is not None and price > 0:
            rows.append((str(raw_date), price))
    rows.sort(key=lambda item: item[0])
    if len(rows) < min_observations:
        return {"status": "INSUFFICIENT_EVIDENCE", "reason": "insufficient point-in-time weekly prices", "observations": len(rows), "dataCutoff": data_cutoff}
    returns = [rows[index][1] / rows[index - 1][1] - 1 for index in range(1, len(rows))]
    mean = sum(returns) / len(returns)
    sigma = math.sqrt(sum((value - mean) ** 2 for value in returns) / max(1, len(returns) - 1)) * math.sqrt(52)
    years = len(rows) / 52
    mu = (rows[-1][1] / rows[0][1]) ** (1 / years) - 1 if years > 0 else None
    candidate = quarterly_half_kelly(mu, sigma)
    return {**candidate, "observations": len(rows), "dataCutoff": data_cutoff, "seriesStart": rows[0][0], "seriesEnd": rows[-1][0]}


def beta_capacity(effective_beta, limit=HALF_KELLY_LIMIT):
    effective_beta = _finite_number(effective_beta)
    limit = _finite_number(limit)
    if effective_beta is None or limit is None or effective_beta < 0 or limit <= 0:
        raise ValueError("Beta and Kelly limit must be non-negative and positive respectively")
    return effective_beta / limit * 100


def beta_status(capacity):
    capacity = _finite_number(capacity)
    if capacity is None:
        return "⚪ 資料不足", "risk-unavailable"
    if capacity >= 115:
        return "🔴 加原型補現金", "risk-alert"
    if capacity >= 95:
        return "🟡 Beta維持", "risk-watch"
    return "🟢 Beta容量良好", "risk-good"


def maintenance_ratio(pledged_value, total_debt):
    pledged_value = _finite_number(pledged_value)
    total_debt = _finite_number(total_debt)
    if pledged_value is None or total_debt is None or pledged_value < 0 or total_debt < 0:
        raise ValueError("Pledged value and debt cannot be negative")
    return pledged_value / total_debt * 100 if total_debt else 0


def maintenance_status(total_debt, ratio):
    total_debt = _finite_number(total_debt)
    ratio = _finite_number(ratio)
    if total_debt is None or ratio is None:
        return "⚪ 資料不足", "risk-unavailable"
    if total_debt <= 0:
        return "✅ 無借款", "risk-good"
    if ratio >= 190:
        return "🟢 維持率充足", "risk-good"
    if ratio >= 167:
        return "🟡 注意槓桿", "risk-watch"
    if ratio >= 150:
        return "🟠 禁止新增槓桿", "risk-orange"
    if ratio >= 130:
        return "🔴 補擔保品", "risk-alert"
    return "🔴 嚴重警示", "risk-critical"


def stressed_maintenance_ratio(pledged_value, pledged_006208_value, total_debt, decline):
    if not 0 <= decline <= 1:
        raise ValueError("Stress decline must be between 0 and 1")
    if pledged_006208_value < 0 or pledged_value < 0:
        raise ValueError("Pledged values cannot be negative")
    stressed_collateral = max(0, pledged_value - pledged_006208_value * decline)
    return maintenance_ratio(stressed_collateral, total_debt)


def stress_scenarios(asset_value, net_asset, pledged_value, pledged_006208_value, total_debt):
    return [
        {
            "label": f"006208 下跌 {int(decline * 100)}%",
            "netImpact": asset_value * -decline,
            "netAsset": net_asset - asset_value * decline,
            "maintenance": stressed_maintenance_ratio(
                pledged_value, pledged_006208_value, total_debt, decline
            ),
        }
        for decline in (0.10, 0.20)
    ]


def composite_guardrails(
    beta_capacity_value,
    maintenance_ratio_value,
    concentration_percent,
    cash_percent,
    *,
    data_fresh=True,
    min_cash_percent=10,
    max_concentration_percent=35,
):
    """Evaluate every safety gate before any risk-increasing suggestion.

    A good Beta capacity is never sufficient by itself. Stale data, weak
    collateral, concentration, or a thin cash buffer make the guardrail fail.
    """
    beta_value = _finite_number(beta_capacity_value)
    maintenance_value = _finite_number(maintenance_ratio_value)
    concentration_value = _finite_number(concentration_percent)
    cash_value = _finite_number(cash_percent)
    rules = [
        {"name": "data_fresh", "passed": bool(data_fresh), "detail": "行情與帳本資料未過期"},
        {"name": "beta_capacity", "passed": beta_value is not None and beta_value < 115, "detail": "Beta 容量低於紅燈線"},
        {"name": "maintenance_ratio", "passed": maintenance_value is not None and (maintenance_value >= 167 or maintenance_value == 0), "detail": "維持率高於加槓桿門檻"},
        {"name": "concentration", "passed": concentration_value is not None and concentration_value < max_concentration_percent, "detail": "單一標的曝險低於警示線"},
        {"name": "cash_safety_buffer", "passed": cash_value is not None and cash_value >= min_cash_percent, "detail": "現金安全墊達最低比例"},
    ]
    eligible = all(rule["passed"] for rule in rules)
    return {
        "eligible": eligible,
        "recommendation": "僅可在政策允許下評估" if eligible else "禁止增加風險，先處理未通過規則",
        "rules": rules,
    }
