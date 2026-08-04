"""Pledged-collateral safety calculations."""


def _distance_to_ratio(pledged_value, debt, threshold):
    if pledged_value <= 0 or debt <= 0:
        return 0.0
    required = debt * threshold / 100
    return max(0.0, (pledged_value - required) / pledged_value * 100)


def pledge_safety_center(
    pledged_value,
    total_debt,
    *,
    warning_ratio=180,
    call_ratio=150,
    stress_decline=0.0,
    pledged_discounts=None,
):
    """Return current, threshold, distance, and top-up metrics."""
    raw_pledged_value = pledged_value
    total_debt = float(total_debt)
    if total_debt < 0:
        raise ValueError("pledged_value and total_debt cannot be negative")
    discounts = pledged_discounts or {}
    if isinstance(raw_pledged_value, dict):
        if any(float(value) < 0 for value in raw_pledged_value.values()):
            raise ValueError("pledged_value and total_debt cannot be negative")
        adjusted_collateral = sum(float(value) * (1 - float(discounts.get(symbol, 0))) for symbol, value in raw_pledged_value.items())
    else:
        adjusted_collateral = float(raw_pledged_value)
        if adjusted_collateral < 0:
            raise ValueError("pledged_value and total_debt cannot be negative")
    current_ratio = adjusted_collateral / total_debt * 100 if total_debt else 0
    stressed_collateral = max(0, adjusted_collateral * (1 - float(stress_decline)))
    stressed_ratio = stressed_collateral / total_debt * 100 if total_debt else 0
    required_top_up = max(0, total_debt * warning_ratio / 100 - adjusted_collateral)
    return {
        "currentRatio": round(current_ratio, 2),
        "warningRatio": warning_ratio,
        "callRatio": call_ratio,
        "distanceToWarningDecline": round(_distance_to_ratio(adjusted_collateral, total_debt, warning_ratio), 2),
        "distanceToCallDecline": round(_distance_to_ratio(adjusted_collateral, total_debt, call_ratio), 2),
        "suggestedTopUp": round(required_top_up, 2),
        "stressDecline": stress_decline,
        "stressRatio": round(stressed_ratio, 2),
        "status": "critical" if current_ratio < call_ratio else "warning" if current_ratio < warning_ratio else "healthy",
    }
