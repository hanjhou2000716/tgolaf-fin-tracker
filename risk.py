"""Pure, testable portfolio-risk calculations used by the dashboard."""

HALF_KELLY_LIMIT = 0.08 / (2 * (0.18 ** 2))


def beta_capacity(effective_beta, limit=HALF_KELLY_LIMIT):
    if effective_beta < 0 or limit <= 0:
        raise ValueError("Beta and Kelly limit must be non-negative and positive respectively")
    return effective_beta / limit * 100


def beta_status(capacity):
    if capacity > 115:
        return "🔴 加原型補現金", "risk-alert"
    if capacity >= 95:
        return "🟡 Beta維持", "risk-watch"
    return "🟢 可加槓桿", "risk-good"


def maintenance_ratio(pledged_value, total_debt):
    if pledged_value < 0 or total_debt < 0:
        raise ValueError("Pledged value and debt cannot be negative")
    return pledged_value / total_debt * 100 if total_debt else 0


def maintenance_status(total_debt, ratio):
    if total_debt <= 0:
        return "✅ 無借款", "risk-good"
    if ratio >= 190:
        return "🟢 可加槓桿", "risk-good"
    if ratio >= 150:
        return "🟡 注意槓桿", "risk-watch"
    return "🔴 補擔保品", "risk-alert"


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
