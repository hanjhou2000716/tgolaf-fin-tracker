"""Goal evaluation and backend forecast contract."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from goal_contract import GOALS, active_goal, evaluate_goal_ladder, fx_quote_is_qualified, target_twd_equivalent
from monte_carlo import goal_probability


def build_goal_forecast(*, state: dict[str, Any], net_asset_twd: float, annual_return: float, annual_volatility: float, as_of: str | date | datetime, fx_quote: dict[str, Any] | None = None, paths: int = 5_000, seed: int = 7) -> dict[str, Any]:
    goal = active_goal(state)
    as_of_day = str(as_of)[:10]
    fx = {
        "required": bool(goal and goal["targetCurrency"] == "USD"),
        "pair": "USD/TWD",
        "rate": (fx_quote or {}).get("rate", (fx_quote or {}).get("price")),
        "asOf": (fx_quote or {}).get("as_of"),
        "fetchedAt": (fx_quote or {}).get("fetched_at"),
        "quality": (fx_quote or {}).get("quality", "unavailable"),
        "fallbackUsed": bool((fx_quote or {}).get("fallback_used", False)),
        "mode": "spot_snapshot",
    }
    if not goal:
        return {"activeGoal": None, "asOf": as_of_day, "horizonDays": 0, "probability": 1.0, "probabilityDefinition": "hit_by_deadline", "paths": paths, "seed": seed, "simulationGranularity": "daily", "achieved": True, "overdue": False, "fx": fx, "status": "completed"}
    target = target_twd_equivalent(goal, fx_quote)
    achieved = any(item.get("goalId") == goal["id"] for item in state.get("achievements", []))
    target_meta = {**goal, "targetTwdEquivalent": target, "displayYear": goal["targetDate"][:4]}
    deadline = date.fromisoformat(goal["targetDate"])
    today = date.fromisoformat(as_of_day)
    horizon_days = max(0, (deadline - today).days)
    overdue = today > deadline
    if achieved:
        probability = 1.0
        model = {"probability": probability, "paths": paths, "seed": seed, "simulationGranularity": "daily", "horizonDays": horizon_days, "probabilityDefinition": "hit_by_deadline"}
    elif target is None or overdue or (goal["targetCurrency"] == "USD" and not fx_quote_is_qualified(fx_quote)):
        model = {"probability": 0.0 if overdue else None, "paths": paths, "seed": seed, "simulationGranularity": "daily", "horizonDays": horizon_days, "probabilityDefinition": "hit_by_deadline"}
    else:
        model = goal_probability(initial=net_asset_twd, target=target, annual_return=annual_return, annual_volatility=annual_volatility, as_of=as_of_day, target_date=goal["targetDate"], paths=paths, seed=seed)
    return {"activeGoal": target_meta, "asOf": as_of_day, "horizonDays": horizon_days, "probability": model.get("probability"), "probabilityDefinition": "hit_by_deadline", "paths": model.get("paths", paths), "seed": model.get("seed", seed), "simulationGranularity": model.get("simulationGranularity", "daily"), "monthlyContribution": 0, "achieved": achieved, "achievedAt": next((item.get("achievedAt") for item in state.get("achievements", []) if item.get("goalId") == goal["id"]), None), "overdue": overdue, "fx": fx}

