"""Deterministic multi-factor portfolio stress scenarios."""


def run_scenario(
    *,
    total_asset,
    net_asset,
    total_debt,
    pledged_value,
    tw_value=0,
    us_value=0,
    nvda_value=0,
    tsmc_value=0,
    tw_shock=0,
    us_shock=0,
    nvda_shock=0,
    tsmc_shock=0,
    fx_shock=0,
    interest_rate_shock=0,
):
    """Apply independent shocks and return decision-support outputs.

    All shocks are decimal returns (``-0.10`` means -10%). NVDA/TSMC shocks
    are applied to their included values; overlapping values are intentionally
    not deducted twice because they are risk annotations, not separate assets.
    """
    total_asset = float(total_asset)
    net_asset = float(net_asset)
    total_debt = float(total_debt)
    debt_after_interest = total_debt * (1 + float(interest_rate_shock))
    tw_base = max(0.0, float(tw_value) - float(tsmc_value))
    us_base = max(0.0, float(us_value) - float(nvda_value))
    tw_after = tw_base * (1 + float(tw_shock)) + float(tsmc_value) * (1 + float(tsmc_shock))
    us_after = (us_base * (1 + float(us_shock)) + float(nvda_value) * (1 + float(nvda_shock))) * (1 + float(fx_shock))
    asset_change = (tw_after - tw_value) + (us_after - us_value)
    new_asset = total_asset + asset_change
    new_net_asset = net_asset + asset_change - (debt_after_interest - total_debt)
    new_pledged = max(0.0, float(pledged_value) + (tw_after - tw_value))
    maintenance = new_pledged / debt_after_interest * 100 if debt_after_interest else 0
    topup = max(0.0, debt_after_interest * 1.8 - new_pledged)
    drawdown = (new_net_asset / net_asset - 1) if net_asset else 0
    return {
        "asset": round(new_asset, 2),
        "netAsset": round(new_net_asset, 2),
        "netAssetChange": round(new_net_asset - net_asset, 2),
        "maintenanceRatio": round(maintenance, 2),
        "drawdown": round(drawdown, 6),
        "topUpRequired": round(topup, 2),
        "shocks": {"tw": tw_shock, "us": us_shock, "nvda": nvda_shock, "tsmc": tsmc_shock, "fx": fx_shock, "interest": interest_rate_shock},
    }
