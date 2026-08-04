"""Constraint-aware portfolio rebalance proposal generator."""


def propose_rebalance(*, holdings, target_weights, total_value, max_single_exposure=.35, min_cash=0, locked=(), trade_unit=1, fees=0) -> dict:
    if total_value <= 0 or trade_unit <= 0:
        raise ValueError("total_value and trade_unit must be positive")
    symbols = set(holdings) | set(target_weights)
    locked = set(locked)
    current_cash = float(holdings.get("CASH", 0))
    proposals = []
    for symbol in sorted(symbols):
        if symbol == "CASH":
            continue
        current = float(holdings.get(symbol, 0))
        desired = float(target_weights.get(symbol, 0)) * float(total_value)
        delta = desired - current
        if symbol in locked:
            continue
        if desired / total_value > max_single_exposure:
            delta = max(0.0, float(max_single_exposure) * total_value - current)
        if abs(delta) < trade_unit:
            continue
        quantity = round(delta / trade_unit) * trade_unit
        if quantity:
            proposals.append({"symbol": symbol, "value": round(quantity, 2), "action": "BUY" if quantity > 0 else "SELL"})
    def cost(plan):
        return sum(abs(item["value"]) for item in plan) + float(fees)
    minimum_cash_plan = [item for item in proposals if item["value"] < 0 or current_cash - cost([item]) >= min_cash]
    risk_plan = sorted(minimum_cash_plan, key=lambda item: abs(item["value"]), reverse=True)
    closest_plan = sorted(minimum_cash_plan, key=lambda item: abs(item["value"]))
    return {"constraints": {"maxSingleExposure": max_single_exposure, "minCash": min_cash, "locked": sorted(locked), "tradeUnit": trade_unit}, "plans": {"minimalTrades": minimum_cash_plan, "lowestRisk": risk_plan, "closestToTarget": closest_plan}}
