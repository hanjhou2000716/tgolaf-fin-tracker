"""Auditable portfolio performance metrics."""

from datetime import date
import math


def time_weighted_return(values, cash_flows=None):
    values = [float(value) for value in values]
    if len(values) < 2 or any(value <= 0 for value in values):
        return 0.0
    flows = list(cash_flows or [0.0] * len(values))
    if len(flows) != len(values):
        raise ValueError("cash_flows must align with values")
    wealth = 1.0
    for start, end, flow in zip(values, values[1:], flows[1:]):
        wealth *= (end - float(flow)) / start
    return wealth - 1.0


def xirr(cash_flows, *, guess=0.1):
    """Solve annualized IRR for ``[(date, amount), ...]`` using Newton steps."""
    if len(cash_flows) < 2:
        return 0.0
    ordered = sorted((item[0], float(item[1])) for item in cash_flows)
    if not any(amount > 0 for _, amount in ordered) or not any(amount < 0 for _, amount in ordered):
        raise ValueError("XIRR requires both positive and negative cash flows")
    first = ordered[0][0]

    def npv(rate):
        return sum(amount / ((1 + rate) ** (((day - first).days) / 365.0)) for day, amount in ordered)

    rate = float(guess)
    for _ in range(100):
        value = npv(rate)
        derivative = sum(
            -((day - first).days / 365.0) * amount
            / ((1 + rate) ** (((day - first).days) / 365.0 + 1))
            for day, amount in ordered
        )
        if abs(derivative) < 1e-12:
            break
        next_rate = rate - value / derivative
        if next_rate <= -0.999999:
            next_rate = (rate - 0.999999) / 2
        if abs(next_rate - rate) < 1e-9:
            return next_rate
        rate = next_rate
    return rate


def max_drawdown(values):
    peak = None
    worst = 0.0
    recovery_days = 0
    current_recovery = 0
    for value in values:
        value = float(value)
        peak = value if peak is None else max(peak, value)
        drawdown = value / peak - 1 if peak else 0
        worst = min(worst, drawdown)
        if drawdown < 0:
            current_recovery += 1
            recovery_days = max(recovery_days, current_recovery)
        else:
            current_recovery = 0
    return worst, recovery_days


def _returns(values):
    return [float(end) / float(start) - 1 for start, end in zip(values, values[1:]) if start]


def summarize_performance(values, *, periods_per_year=252):
    values = [float(value) for value in values]
    returns = _returns(values)
    if not values or not returns:
        return {"twr": 0.0, "annualizedReturn": 0.0, "annualizedVolatility": 0.0, "sharpe": 0.0, "sortino": 0.0, "calmar": 0.0, "maxDrawdown": 0.0, "recoveryDays": 0}
    twr = time_weighted_return(values)
    annualized_return = (1 + twr) ** (periods_per_year / len(returns)) - 1 if twr > -1 else -1
    mean = sum(returns) / len(returns)
    variance = sum((item - mean) ** 2 for item in returns) / max(1, len(returns) - 1)
    volatility = math.sqrt(variance) * math.sqrt(periods_per_year)
    downside = [item for item in returns if item < 0]
    downside_dev = math.sqrt(sum(item * item for item in downside) / len(downside)) * math.sqrt(periods_per_year) if downside else 0
    sharpe = annualized_return / volatility if volatility else 0
    sortino = annualized_return / downside_dev if downside_dev else 0
    drawdown, recovery_days = max_drawdown(values)
    calmar = annualized_return / abs(drawdown) if drawdown else 0
    return {
        "twr": round(twr, 8),
        "annualizedReturn": round(annualized_return, 8),
        "annualizedVolatility": round(volatility, 8),
        "sharpe": round(sharpe, 8),
        "sortino": round(sortino, 8),
        "calmar": round(calmar, 8),
        "maxDrawdown": round(drawdown, 8),
        "recoveryDays": recovery_days,
    }
