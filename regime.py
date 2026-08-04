"""Small, explainable market-regime classifier (not a trading signal)."""

import math


def classify_regime(values, *, short_window=20, long_window=60) -> dict:
    values = [float(value) for value in values]
    if len(values) < 3 or any(value <= 0 for value in values):
        raise ValueError("at least three positive observations are required")
    returns = [b / a - 1 for a, b in zip(values, values[1:])]
    short = values[-min(short_window, len(values)):]
    long = values[-min(long_window, len(values)):]
    short_mean = sum(short) / len(short)
    long_mean = sum(long) / len(long)
    trend = "bull" if short_mean > long_mean * 1.01 else "bear" if short_mean < long_mean * 0.99 else "neutral"
    vol = math.sqrt(sum((item - sum(returns) / len(returns)) ** 2 for item in returns) / max(1, len(returns) - 1)) * math.sqrt(252)
    volatility = "high" if vol > 0.30 else "normal" if vol > 0.15 else "low"
    confidence = min(0.99, max(0.5, abs(short_mean / long_mean - 1) * 20 + (0.2 if volatility != "normal" else 0.0)))
    return {
        "trend": trend,
        "volatility": volatility,
        "confidence": round(confidence, 4),
        "features": {"shortMean": round(short_mean, 4), "longMean": round(long_mean, 4), "annualizedVolatility": round(vol, 6)},
        "modelVersion": "regime-v1",
    }
