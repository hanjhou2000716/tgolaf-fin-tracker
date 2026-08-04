"""Runtime integration for Luna's forecast, health and advisor contracts."""

import math

from advisor import build_advice
from data_health import build_data_health
from forecast_quality import margin_call_probability
from monte_carlo import goal_probability
from performance_report import build_performance_report
from regime import classify_regime


def build_runtime_extensions(*, net_values, net_asset, total_asset, total_debt, pledged_value, data_as_of, sources, reconciled=True, now=None) -> dict:
    """Build private-only extensions from the same snapshot used by the UI.

    Each section is deliberately decision-support data. A missing/short price
    history yields ``None`` instead of a fabricated prediction.
    """
    values = [float(value) for value in net_values if float(value) > 0]
    performance = build_performance_report(values) if len(values) >= 2 else None
    regime = classify_regime(values) if len(values) >= 3 else None
    annual_return = float(performance["annualizedReturn"]) if performance else 0.0
    volatility = float(performance["annualizedVolatility"]) if performance else 0.0
    forecast = None
    if net_asset > 0:
        forecast = goal_probability(
            initial=float(net_asset),
            target=max(float(net_asset), 10_000_000),
            annual_return=max(-0.95, min(1.5, annual_return)),
            annual_volatility=max(0.0, min(2.0, volatility)),
            months=60,
            paths=250,
            seed=7,
        )
    daily_volatility = volatility / math.sqrt(252) if volatility else 0.0
    margin = {
        str(horizon): margin_call_probability(
            current_ratio=max(float(pledged_value) / float(total_debt) * 100 if total_debt else 0, 0.01),
            daily_volatility=daily_volatility,
            horizon_days=horizon,
        )
        for horizon in (1, 5, 20, 60)
    }
    health = build_data_health(
        last_sync=data_as_of,
        sources=sources,
        missing=[] if total_asset > 0 else ["portfolio_value"],
        pending_transactions=0,
        reconciled=reconciled,
        now=now,
    )
    advisor = build_advice(
        action="維持現況並持續觀察",
        reason="系統先提供可解釋的風險與機率估計，不自動產生交易指令",
        expected_improvement="保留人工審核與風控邊界",
        side_effects="不會自動調整資產配置",
        data_as_of=data_as_of,
        confidence=0.5 if health["stale"] else 0.7,
        before={"netAsset": round(float(net_asset), 2)},
        after={"netAsset": round(float(net_asset), 2)},
        guardrails={"dataFresh": not health["stale"], "reconciled": bool(reconciled)},
    )
    return {
        "performanceReport": performance,
        "regime": regime,
        "goalForecast": forecast,
        "marginCallProbability": margin,
        "dataHealth": health,
        "advisor": advisor,
    }
