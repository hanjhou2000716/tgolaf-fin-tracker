"""Pledge safety center contract for the Risk Center UI."""

from pledge_safety import pledge_safety_center


def build_pledge_risk_center(*, collateral, debt, warning_ratio=180, call_ratio=150, stress_decline=0.0, discounts=None) -> dict:
    """Build a complete, decision-support-only collateral report.

    ``collateral`` may be a total value or a mapping of collateral symbol to
    value. Discounts are decimal haircuts (0.2 means a 20% haircut). The
    function never recommends adding leverage; it only reports guardrails and
    the cash needed to restore the warning line.
    """
    adjusted_collateral = collateral
    if isinstance(collateral, dict):
        adjusted_collateral = sum(float(value) * (1 - float((discounts or {}).get(symbol, 0))) for symbol, value in collateral.items())
    report = pledge_safety_center(
        adjusted_collateral,
        debt,
        warning_ratio=warning_ratio,
        call_ratio=call_ratio,
        stress_decline=stress_decline,
        pledged_discounts=None,
    )
    report["guardrails"] = {
        "currentAboveWarning": report["currentRatio"] >= warning_ratio,
        "stressAboveWarning": report["stressRatio"] >= warning_ratio,
        "stressAboveCall": report["stressRatio"] >= call_ratio,
        "leverageIncreaseAllowed": False,
    }
    report["status"] = "CRITICAL" if report["currentRatio"] < call_ratio else ("WARNING" if report["currentRatio"] < warning_ratio else "SAFE")
    return report
