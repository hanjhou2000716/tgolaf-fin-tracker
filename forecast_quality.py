"""追繳機率估計與預測校準指標。"""

import math


def _normal_cdf(value):
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))


def margin_call_probability(*, current_ratio, daily_volatility, horizon_days, threshold=150) -> dict:
    """Approximate probability ratio falls below threshold via log-normal shock."""
    if current_ratio <= 0 or daily_volatility < 0 or horizon_days <= 0 or threshold <= 0:
        raise ValueError("ratio/volatility/horizon/threshold must be positive")
    sigma = float(daily_volatility) * math.sqrt(horizon_days)
    if sigma == 0:
        probability = 1.0 if current_ratio < threshold else 0.0
    else:
        z = math.log(float(threshold) / float(current_ratio)) / sigma
        probability = _normal_cdf(z)
    return {"horizonDays": horizon_days, "threshold": threshold, "probability": round(probability, 6), "modelVersion": "margin-probability-v1"}


def calibrate_quantiles(predicted: dict, actuals) -> dict:
    """Return empirical coverage and pinball loss for P5..P95 forecasts."""
    actuals = [float(value) for value in actuals]
    if not actuals:
        raise ValueError("actuals cannot be empty")
    result = {}
    for label, quantile in (("P5", .05), ("P25", .25), ("P50", .5), ("P75", .75), ("P95", .95)):
        forecast = float(predicted[label])
        coverage = sum(actual >= forecast for actual in actuals) / len(actuals)
        loss = sum((quantile - (1 if actual < forecast else 0)) * (actual - forecast) for actual in actuals) / len(actuals)
        result[label] = {"coverage": round(coverage, 6), "pinballLoss": round(loss, 6), "targetCoverage": quantile}
    return result
