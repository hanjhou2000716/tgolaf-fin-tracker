"""Production Buy&Hold market-light policy.

This module deliberately contains policy and presentation data only.  It does
not import the research engines, portfolio accounting, order execution, or
Telegram clients.  The dashboard pipeline supplies the completed-session
TAIEX history and current portfolio values, and this module returns a small,
auditable contract for the daily summary and private Mini App.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import math
from typing import Any, Iterable, Mapping, Sequence


LIGHT_ORDER: tuple[str, ...] = ("BLUE", "GREEN", "YELLOW", "ORANGE", "RED")
LIGHT_DEFINITIONS: dict[str, dict[str, Any]] = {
    "BLUE": {"name": "藍燈", "emoji": "🔵", "meaning": "正常持有", "threshold": -0.05, "action": "維持持有"},
    "GREEN": {"name": "綠燈", "emoji": "🟢", "meaning": "原型買進區", "threshold": -0.08, "action": "本月可買 006208", "budget_pct": 0.0},
    "YELLOW": {"name": "黃燈", "emoji": "🟡", "meaning": "初階加碼區", "threshold": -0.12, "action": "2% NAV｜90:10", "budget_pct": 0.02, "target_allocation": {"006208": 0.90, "00685L": 0.10}},
    "ORANGE": {"name": "橘燈", "emoji": "🟠", "meaning": "深度加碼區", "threshold": -0.18, "action": "3% NAV｜80:20", "budget_pct": 0.03, "target_allocation": {"006208": 0.80, "00685L": 0.20}},
    "RED": {"name": "紅燈", "emoji": "🔴", "meaning": "極端加碼區", "threshold": float("-inf"), "action": "5% NAV｜70:30", "budget_pct": 0.05, "target_allocation": {"006208": 0.70, "00685L": 0.30}},
}
TIER_BUDGET_PCT = {"YELLOW": 0.02, "ORANGE": 0.03, "RED": 0.05}
TIER_ALLOCATION = {
    "YELLOW": {"006208": 0.90, "00685L": 0.10},
    "ORANGE": {"006208": 0.80, "00685L": 0.20},
    "RED": {"006208": 0.70, "00685L": 0.30},
}
# Entry boundary of each light when moving deeper.  The definition threshold
# above is the lower bound of that light; the ladder's next trigger uses the
# upper boundary of the current light (e.g. Green -> Yellow at -8%).
LIGHT_ENTRY_THRESHOLDS = {"GREEN": -0.05, "YELLOW": -0.08, "ORANGE": -0.12, "RED": -0.18}
RESET_DD = -0.01
RESET_SESSIONS = 3
HYSTERESIS_POINTS = 0.02
HYSTERESIS_SESSIONS = 3
CASH_FLOOR_PCT = 0.03
DEBT_TO_NET_ASSET_LIMIT = 0.30
MAINTENANCE_RATIO_LIMIT = 167.0
POSITION_00685L_CAP_PCT = 0.10


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _history_rows(history: Any) -> list[dict[str, Any]]:
    """Convert pandas-like or plain history input to sorted close rows."""
    rows: list[dict[str, Any]] = []
    if history is None:
        return rows
    if hasattr(history, "iterrows"):
        for index, row in history.iterrows():
            item = row.to_dict() if hasattr(row, "to_dict") else dict(row)
            item.setdefault("date", index)
            rows.append(item)
    elif isinstance(history, Mapping):
        rows = [dict(history)]
    else:
        for item in history:
            if isinstance(item, Mapping):
                rows.append(dict(item))
            elif isinstance(item, Sequence) and len(item) >= 2:
                rows.append({"date": item[0], "close": item[1]})
    normalized: list[dict[str, Any]] = []
    for item in rows:
        item_date = _date_value(item.get("date", item.get("Date", item.get("timestamp"))))
        close = _finite(item.get("close", item.get("Close")))
        if item_date is None or close is None or close <= 0:
            continue
        normalized.append({**item, "date": item_date, "close": close})
    normalized.sort(key=lambda item: item["date"])
    deduped: dict[date, dict[str, Any]] = {}
    for item in normalized:
        deduped[item["date"]] = item
    return list(deduped.values())


def completed_taiex_rows(history: Any, *, as_of: datetime | date | None = None) -> list[dict[str, Any]]:
    """Return valid completed-session rows, excluding a still-open session."""
    rows = _history_rows(history)
    if as_of is None:
        return rows
    if isinstance(as_of, datetime):
        local_date = as_of.date()
        # Taiwan's cash session is complete after 14:00 local time.  Before
        # that point today's provider row is not a completed-session close.
        include_today = as_of.hour >= 14
    else:
        local_date = as_of
        include_today = False
    return [item for item in rows if item["date"] < local_date or (include_today and item["date"] == local_date)]


def calculate_taiex_dd240(history: Any, *, as_of: datetime | date | None = None) -> dict[str, Any]:
    """Calculate the latest 240-trading-session TAIEX drawdown."""
    rows = completed_taiex_rows(history, as_of=as_of)
    if len(rows) < 240:
        return {"status": "UNAVAILABLE", "reason": "TAIEX history < 240 sessions", "sessions": len(rows)}
    window = rows[-240:]
    current = window[-1]
    highest = max(item["close"] for item in window)
    drawdown = current["close"] / highest - 1.0 if highest > 0 else None
    if drawdown is None or not math.isfinite(drawdown):
        return {"status": "UNAVAILABLE", "reason": "DD240 calculation failed", "sessions": len(rows)}
    return {
        "status": "READY",
        "date": current["date"].isoformat(),
        "close": current["close"],
        "highest240": highest,
        "dd240": drawdown,
        "dd240Pct": drawdown * 100.0,
        "sessions": len(rows),
    }


def classify_market_light(dd240: Any) -> str:
    value = _finite(dd240)
    if value is None:
        return "UNAVAILABLE"
    # Avoid provider/calculation floating-point noise at locked boundaries
    # such as exactly -5%, -8%, -12%, and -18%.
    value = round(value, 12)
    if value > -0.05:
        return "BLUE"
    if value > -0.08:
        return "GREEN"
    if value > -0.12:
        return "YELLOW"
    if value > -0.18:
        return "ORANGE"
    return "RED"


def _rank(light: str) -> int:
    try:
        return LIGHT_ORDER.index(light)
    except ValueError:
        return -1


def next_light_details(light: str, highest240: Any, current_close: Any) -> dict[str, Any]:
    """Return the next locked ladder threshold and price distance."""
    normalized = str(light or "UNAVAILABLE").upper()
    next_code = {"BLUE": "GREEN", "GREEN": "YELLOW", "YELLOW": "ORANGE", "ORANGE": "RED"}.get(normalized)
    high = _finite(highest240)
    close = _finite(current_close)
    if not next_code or high is None or close is None or close <= 0:
        return {"code": None, "name": "目前已達最高機會級別" if normalized == "RED" else "—", "triggerPrice": None, "distancePct": None, "distancePoints": None}
    threshold = float(LIGHT_ENTRY_THRESHOLDS[next_code])
    trigger = high * (1.0 + threshold)
    return {
        "code": next_code,
        "name": LIGHT_DEFINITIONS[next_code]["name"],
        "threshold": threshold,
        "triggerPrice": trigger,
        "distancePct": trigger / close - 1.0,
        "distancePoints": trigger - close,
    }


def _nav_for_date(nav_history: Any, item_date: date, fallback: float | None) -> float | None:
    if nav_history is None:
        return fallback
    if hasattr(nav_history, "iterrows"):
        nav_rows = nav_history.to_dict("records")
    elif isinstance(nav_history, Mapping):
        nav_rows = [nav_history]
    else:
        nav_rows = list(nav_history)
    for row in reversed(nav_rows):
        if not isinstance(row, Mapping):
            continue
        row_date = _date_value(row.get("date", row.get("Date")))
        value = _finite(row.get("net_asset", row.get("Net_Asset", row.get("netAsset"))))
        if row_date == item_date and value is not None and value > 0:
            return value
    return fallback


def _raw_light_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        if index < 239:
            continue
        window = rows[index - 239 : index + 1]
        high = max(row["close"] for row in window)
        dd = item["close"] / high - 1.0
        output.append({"date": item["date"], "close": item["close"], "highest240": high, "dd240": dd, "light": classify_market_light(dd)})
    return output


def build_episode_policy(
    history: Any,
    *,
    net_asset: Any = None,
    nav_history: Any = None,
    as_of: datetime | date | None = None,
    prior_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build current light, episode de-dup state, and recommendation policy."""
    rows = completed_taiex_rows(history, as_of=as_of)
    series = _raw_light_series(rows)
    if not series:
        return {"status": "UNAVAILABLE", "reason": "TAIEX history < 240 sessions", "state": "DATA_UNAVAILABLE"}
    state = dict(prior_state or {})
    episode_id = state.get("episodeId")
    episode_base_nav = _finite(state.get("episodeBaseNav"))
    triggered = set(str(item) for item in (state.get("triggeredTiers") or []))
    effective = str(state.get("effectiveLight") or "BLUE").upper()
    downgrade_streak = int(state.get("downgradeStreak") or 0)
    reset_streak = int(state.get("resetStreak") or 0)
    events: list[dict[str, Any]] = []
    previous_raw = None
    for item in series:
        raw = item["light"]
        raw_rank = _rank(raw)
        effective_rank = _rank(effective)
        if item["dd240"] >= RESET_DD:
            reset_streak += 1
        else:
            reset_streak = 0
        if reset_streak >= RESET_SESSIONS:
            episode_id = None
            episode_base_nav = None
            triggered.clear()
            effective = raw
            downgrade_streak = 0
        elif raw_rank > effective_rank:
            effective = raw
            downgrade_streak = 0
        elif raw_rank < effective_rank and effective_rank > 0:
            downgrade_threshold = float(LIGHT_ENTRY_THRESHOLDS.get(effective, -0.05)) + HYSTERESIS_POINTS
            if item["dd240"] > downgrade_threshold:
                downgrade_streak += 1
                if downgrade_streak >= HYSTERESIS_SESSIONS:
                    effective = raw
                    downgrade_streak = 0
            else:
                downgrade_streak = 0
        else:
            downgrade_streak = 0

        current_rank = _rank(raw)
        prior_rank = _rank(previous_raw) if previous_raw else -1
        if current_rank >= _rank("YELLOW"):
            if episode_id is None:
                episode_id = f"{item['date'].isoformat()}-episode"
                episode_base_nav = _nav_for_date(nav_history, item["date"], _finite(net_asset))
                triggered.clear()
            start_tier = max(_rank("YELLOW"), prior_rank + 1)
            for tier in LIGHT_ORDER[start_tier : current_rank + 1]:
                if tier not in TIER_BUDGET_PCT or tier in triggered:
                    continue
                triggered.add(tier)
                events.append({
                    "date": item["date"].isoformat(),
                    "tier": tier,
                    "episodeId": episode_id,
                    "episodeBaseNav": episode_base_nav,
                    "budgetPct": TIER_BUDGET_PCT[tier],
                    "targetAllocation": dict(TIER_ALLOCATION[tier]),
                    "executed": False,
                })
        previous_raw = raw

    latest = series[-1]
    # ``effective`` is the displayed light after the locked +2pp/3-session
    # downgrade hysteresis.  Upgrades remain immediate; rawLight is retained
    # for auditability without exposing extra debug data in the UI.
    light = effective if effective in LIGHT_DEFINITIONS else classify_market_light(latest["dd240"])
    details = LIGHT_DEFINITIONS.get(light)
    next_details = next_light_details(light, latest["highest240"], latest["close"])
    base_nav = episode_base_nav
    tier_budget = float(details.get("budget_pct", 0.0)) * base_nav if details and base_nav else 0.0
    monthly_rows = [item for item in series if item["date"].year == latest["date"].year and item["date"].month == latest["date"].month]
    monthly_priority = any(_rank(item["light"]) >= _rank("GREEN") for item in monthly_rows)
    return {
        "status": "READY",
        "state": "READY",
        "light": {
            "code": light,
            "rawLight": latest["light"],
            "name": details["name"],
            "emoji": details["emoji"],
            "meaning": details["meaning"],
            "dd240": latest["dd240"],
            "dd240Pct": latest["dd240"] * 100.0,
            "currentClose": latest["close"],
            "highest240": latest["highest240"],
            "signalDate": latest["date"].isoformat(),
            "nextLight": next_details,
        },
        "recommendation": {
            "action": details["action"],
            "monthlyDca006208Allowed": True,
            "monthlyDcaPriority": monthly_priority,
            "theoreticalBudget": tier_budget,
            "episodeBaseNav": base_nav,
        },
        "episode": {
            "episodeId": episode_id,
            "episodeStartDate": episode_id.split("-episode")[0] if episode_id else None,
            "episodeBaseNav": base_nav,
            "triggeredTiers": sorted(triggered, key=_rank),
            "events": events,
            "totalBudgetUsed": 0.0,
            "maxBudgetPct": 0.10,
        },
        "marketContract": {"benchmark": "TAIEX", "signal": "240 Trading-Day Drawdown", "timing": "completed-session-close"},
    }


def evaluate_portfolio_gate(
    *,
    net_asset: Any,
    total_cash: Any,
    total_debt: Any,
    maintenance_ratio: Any = None,
    pledged_value_available: bool | None = None,
    current_00685l_value: Any = 0,
    desired_budget: Any = 0,
    target_00685l_pct: Any = 0,
) -> dict[str, Any]:
    """Apply cash, debt, pledge, and 00685L cap gates without placing orders."""
    nav = _finite(net_asset) or 0.0
    cash = _finite(total_cash) or 0.0
    debt = _finite(total_debt) or 0.0
    current_leveraged = max(0.0, _finite(current_00685l_value) or 0.0)
    requested = max(0.0, _finite(desired_budget) or 0.0)
    cash_floor = max(0.0, nav * CASH_FLOOR_PCT)
    deployable = max(0.0, cash - cash_floor)
    debt_ratio = debt / nav if nav > 0 else None
    debt_pass = debt_ratio is not None and debt_ratio < DEBT_TO_NET_ASSET_LIMIT
    ratio = _finite(maintenance_ratio)
    if debt <= 0:
        maintenance_state = "PASS"
    elif ratio is None or pledged_value_available is False:
        maintenance_state = "DATA UNAVAILABLE"
    else:
        maintenance_state = "PASS" if ratio >= MAINTENANCE_RATIO_LIMIT else "正二受限"
    cash_state = "PASS" if cash >= cash_floor else "現金不足"
    six_eighty_five_cap = max(0.0, nav * POSITION_00685L_CAP_PCT)
    cap_remaining = max(0.0, six_eighty_five_cap - current_leveraged)
    target_pct = min(1.0, max(0.0, _finite(target_00685l_pct) or 0.0))
    executable = min(requested, deployable)
    leveraged_requested = executable * target_pct
    leveraged_allowed = min(leveraged_requested, cap_remaining) if debt_pass and maintenance_state == "PASS" else 0.0
    redirected = max(0.0, leveraged_requested - leveraged_allowed)
    stock_006208 = max(0.0, executable - leveraged_allowed)
    blockers: list[str] = []
    if cash_state != "PASS" and requested > 0:
        blockers.append(cash_state)
    if target_pct > 0 and not debt_pass:
        blockers.append("正二受限")
    if target_pct > 0 and maintenance_state != "PASS":
        blockers.append(maintenance_state)
    if target_pct > 0 and leveraged_allowed + 1e-9 < leveraged_requested and debt_pass and maintenance_state == "PASS":
        blockers.append("部分限制")
    if requested > executable + 1e-9:
        blockers.append("部分限制")
    if not nav or (target_pct > 0 and debt_ratio is None):
        status = "DATA UNAVAILABLE"
    elif blockers:
        status = "／".join(dict.fromkeys(blockers))
    else:
        status = "PASS"
    return {
        "status": status,
        "cash": {"state": cash_state, "available": cash, "floor": cash_floor, "deployable": deployable},
        "debt": {"state": "PASS" if debt_pass else "正二受限", "debt": debt, "debtToNetAsset": debt_ratio},
        "maintenance": {"state": maintenance_state, "ratio": ratio, "required": MAINTENANCE_RATIO_LIMIT},
        "00685L": {"state": "PASS" if leveraged_allowed > 0 or target_pct == 0 else "BLOCKED", "cap": six_eighty_five_cap, "currentValue": current_leveraged, "remainingCap": cap_remaining, "requested": leveraged_requested, "allowed": leveraged_allowed},
        "requestedBudget": requested,
        "executableBudget": executable,
        "allocation": {"006208": stock_006208, "00685L": leveraged_allowed, "redirectedTo006208": redirected},
        "autoBorrowing": False,
    }


def build_buy_hold_policy(
    history: Any,
    *,
    net_asset: Any = None,
    nav_history: Any = None,
    total_cash: Any = 0,
    total_debt: Any = 0,
    maintenance_ratio: Any = None,
    pledged_value_available: bool | None = None,
    current_00685l_value: Any = 0,
    as_of: datetime | date | None = None,
    prior_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    policy = build_episode_policy(history, net_asset=net_asset, nav_history=nav_history, as_of=as_of, prior_state=prior_state)
    if policy.get("status") != "READY":
        return {
            "status": "UNAVAILABLE",
            "state": "DATA_UNAVAILABLE",
            "light": {"code": "UNAVAILABLE", "name": "資料暫不可用", "emoji": "⚪", "meaning": "Opportunity Buy disabled", "dd240": None, "nextLight": {"code": None, "name": "—"}},
            "recommendation": {"action": "Opportunity Buy disabled", "monthlyDca006208Allowed": False, "monthlyDcaPriority": False, "theoreticalBudget": 0.0},
            "portfolioGate": {"status": "DATA UNAVAILABLE", "autoBorrowing": False},
            "reason": policy.get("reason", "market data unavailable"),
            "marketContract": {"benchmark": "TAIEX", "signal": "240 Trading-Day Drawdown", "timing": "completed-session-close"},
        }
    light = policy["light"]["code"]
    target = TIER_ALLOCATION.get(light, {})
    gate = evaluate_portfolio_gate(
        net_asset=net_asset,
        total_cash=total_cash,
        total_debt=total_debt,
        maintenance_ratio=maintenance_ratio,
        pledged_value_available=pledged_value_available,
        current_00685l_value=current_00685l_value,
        desired_budget=policy["recommendation"].get("theoreticalBudget", 0.0),
        target_00685l_pct=target.get("00685L", 0.0),
    )
    policy["portfolioGate"] = gate
    policy["status"] = "READY"
    policy["state"] = "READY"
    return policy


def classify_light(dd240: Any) -> str:
    """Readable alias used by callers and tests."""
    return classify_market_light(dd240)


def build_portfolio_gate(**kwargs: Any) -> dict[str, Any]:
    return evaluate_portfolio_gate(**kwargs)


def buy_hold_telegram_line(policy: Mapping[str, Any]) -> str:
    light = policy.get("light") or {}
    code = str(light.get("code") or "UNAVAILABLE").upper()
    if code == "UNAVAILABLE":
        return "🚦 Buy&Hold：⚪ 資料暫不可用"
    meaning = str(light.get("meaning") or "Opportunity Buy disabled")
    # Keep the daily message to one line while carrying the locked policy
    # context required by the V1 contract.  Blue is a hold state; the deeper
    # tiers include only their incremental budget (no private NAV amount).
    suffix = {
        "GREEN": "本月可買 006208",
        "YELLOW": "2% NAV",
        "ORANGE": "3% NAV",
        "RED": "5% NAV",
    }.get(code)
    if suffix:
        meaning = f"{meaning}（{suffix}）"
    return f"🚦 Buy&Hold：{light.get('emoji', '⚪')} {light.get('name', '資料暫不可用')}｜{meaning}"
