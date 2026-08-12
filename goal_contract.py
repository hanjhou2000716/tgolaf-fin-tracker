"""Canonical long-term wealth goals and private achievement state machine."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any


GOALS = (
    {"id": "G1_TWD_10M", "sequence": 1, "targetAmount": 10_000_000, "targetCurrency": "TWD", "targetDate": "2028-07-16"},
    {"id": "G2_USD_1M", "sequence": 2, "targetAmount": 1_000_000, "targetCurrency": "USD", "targetDate": "2038-07-16"},
    {"id": "G3_TWD_100M", "sequence": 3, "targetAmount": 100_000_000, "targetCurrency": "TWD", "targetDate": "2048-07-16"},
)
GOAL_BY_ID = {goal["id"]: goal for goal in GOALS}


def initial_goal_state() -> dict[str, Any]:
    return {
        "activeGoalId": GOALS[0]["id"],
        "achievements": [],
        "goals": [
            {
                "goalId": goal["id"],
                "sequence": goal["sequence"],
                "status": "active" if goal["sequence"] == 1 else "pending",
                "achieved": False,
                "achievedAt": None,
                "achievedNetAssetTwd": None,
            }
            for goal in GOALS
        ],
        "status": "active",
        "completedAt": None,
    }


def _parse_day(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _iso_now(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.replace(tzinfo=timezone.utc).isoformat()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    return str(value)


def fx_quote_is_qualified(quote: dict[str, Any] | None) -> bool:
    """Only fresh, non-fallback, positive USD/TWD quotes may persist G2 achievement."""
    if not quote:
        return False
    try:
        rate = float(quote.get("rate", quote.get("price")))
    except (TypeError, ValueError):
        return False
    quality = str(quote.get("quality") or "").lower()
    return rate > 0 and quality in {"fresh", "trusted", "ok"} and not bool(quote.get("is_stale")) and not bool(quote.get("fallback_used"))


def target_twd_equivalent(goal: dict[str, Any], fx_quote: dict[str, Any] | None = None) -> float | None:
    if goal["targetCurrency"] == "TWD":
        return float(goal["targetAmount"])
    if not fx_quote:
        return None
    try:
        rate = float(fx_quote.get("rate", fx_quote.get("price")))
    except (TypeError, ValueError):
        return None
    return float(goal["targetAmount"]) * rate if rate > 0 else None


def _achievement(state: dict[str, Any], goal: dict[str, Any]) -> dict[str, Any] | None:
    return next((item for item in state.get("achievements", []) if item.get("goalId") == goal["id"]), None)


def _can_achieve(goal: dict[str, Any], net_asset_twd: float, fx_quote: dict[str, Any] | None) -> tuple[bool, float | None]:
    equivalent = target_twd_equivalent(goal, fx_quote)
    if equivalent is None or net_asset_twd < equivalent:
        return False, equivalent
    if goal["targetCurrency"] == "USD" and not fx_quote_is_qualified(fx_quote):
        return False, equivalent
    return True, equivalent


def evaluate_goal_ladder(*, state: dict[str, Any] | None, net_asset_twd: float, as_of: str | date | datetime, fx_quote: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply monotonic achievement transitions, including multi-goal crossings."""
    current = deepcopy(state or initial_goal_state())
    current.setdefault("achievements", [])
    current.setdefault("goals", [])
    record_by_id = {record.get("goalId"): record for record in current["goals"] if record.get("goalId")}
    for goal in GOALS:
        record_by_id.setdefault(goal["id"], {
            "goalId": goal["id"], "sequence": goal["sequence"], "status": "pending",
            "achieved": False, "achievedAt": None, "achievedNetAssetTwd": None,
        })
    current["goals"] = [record_by_id[goal["id"]] for goal in GOALS]
    current.setdefault("status", "active")
    now_iso = _iso_now(as_of)
    for goal in GOALS:
        if _achievement(current, goal):
            record = record_by_id[goal["id"]]
            record.update({"status": "achieved", "achieved": True})
            continue
        achieved, equivalent = _can_achieve(goal, float(net_asset_twd), fx_quote)
        if not achieved:
            record_by_id[goal["id"]].update({"status": "active", "achieved": False})
            for later in GOALS[goal["sequence"]:]:
                record_by_id[later["id"]].update({"status": "pending", "achieved": False})
            current["activeGoalId"] = goal["id"]
            current["status"] = "active"
            break
        record = {"goalId": goal["id"], "sequence": goal["sequence"], "status": "achieved", "achievedAt": now_iso, "achievedNetAssetTwd": round(float(net_asset_twd), 2)}
        if goal["targetCurrency"] == "USD":
            record["achievedFxRate"] = float(fx_quote.get("rate", fx_quote.get("price")))
            record["achievedTargetTwdEquivalent"] = round(float(equivalent), 2)
        current["achievements"].append(record)
        record_by_id[goal["id"]].update(record, achieved=True)
    else:
        current["activeGoalId"] = None
        current["status"] = "completed"
        current["completedAt"] = current.get("completedAt") or now_iso
    return current


def active_goal(state: dict[str, Any] | None) -> dict[str, Any] | None:
    active_id = (state or {}).get("activeGoalId")
    return GOAL_BY_ID.get(active_id) if active_id else None
