"""Explainable, guardrail-aware recommendation cards (no auto-trading)."""


def build_advice(*, action, reason, expected_improvement, side_effects, data_as_of, confidence, before, after, guardrails=None) -> dict:
    if not action or not reason or not data_as_of:
        raise ValueError("action, reason and data_as_of are required")
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    failed = [name for name, passed in (guardrails or {}).items() if not passed]
    return {
        "action": action,
        "reason": reason,
        "expectedImprovement": expected_improvement,
        "sideEffects": side_effects,
        "dataAsOf": data_as_of,
        "confidence": round(confidence, 4),
        "before": before,
        "after": after,
        "failedGuardrails": failed,
        "requiresApproval": True,
        "isTradeInstruction": False,
    }
