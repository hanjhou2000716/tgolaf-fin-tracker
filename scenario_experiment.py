"""Validated scenario-lab input contract and before/after comparison."""

from scenario_lab import run_scenario


SHOCK_KEYS = ("tw", "us", "nvda", "tsmc", "fx", "interest")


def run_adjustable_scenario(*, portfolio: dict, shocks: dict) -> dict:
    """Run a user-adjustable market scenario with explicit shock bounds."""
    unknown = set(shocks) - set(SHOCK_KEYS)
    if unknown:
        raise ValueError(f"unsupported shock keys: {sorted(unknown)}")
    normalized = {key: float(shocks.get(key, 0.0)) for key in SHOCK_KEYS}
    if any(value < -1 or value > 3 for value in normalized.values()):
        raise ValueError("shocks must be between -100% and +300%")
    required = ("total_asset", "net_asset", "total_debt", "pledged_value")
    missing = [key for key in required if key not in portfolio]
    if missing:
        raise ValueError(f"missing portfolio fields: {missing}")
    kwargs = {
        "total_asset": portfolio["total_asset"], "net_asset": portfolio["net_asset"],
        "total_debt": portfolio["total_debt"], "pledged_value": portfolio["pledged_value"],
        "tw_value": portfolio.get("tw_value", 0), "us_value": portfolio.get("us_value", 0),
        "nvda_value": portfolio.get("nvda_value", 0), "tsmc_value": portfolio.get("tsmc_value", 0),
        "tw_shock": normalized["tw"], "us_shock": normalized["us"],
        "nvda_shock": normalized["nvda"], "tsmc_shock": normalized["tsmc"],
        "fx_shock": normalized["fx"], "interest_rate_shock": normalized["interest"],
    }
    baseline = run_scenario(**{key: value for key, value in kwargs.items() if key.endswith("_value") or key in {"total_asset", "net_asset", "total_debt", "pledged_value"}})
    result = run_scenario(**kwargs)
    result["baseline"] = baseline
    result["guardrails"] = {
        "maintenanceAbove150": result["maintenanceRatio"] >= 150,
        "netAssetPositive": result["netAsset"] > 0,
        "topUpRequired": result["topUpRequired"] > 0,
    }
    return result
