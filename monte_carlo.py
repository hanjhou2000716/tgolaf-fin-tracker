"""Seeded Monte Carlo wealth and goal probability estimates."""

import random
from datetime import date


def _quantiles(values):
    values = sorted(values)
    def q(percent):
        index = (len(values) - 1) * percent
        low, high = int(index), min(len(values) - 1, int(index) + 1)
        return values[low] + (values[high] - values[low]) * (index - low)
    return {"P5": round(q(.05), 2), "P25": round(q(.25), 2), "P50": round(q(.50), 2), "P75": round(q(.75), 2), "P95": round(q(.95), 2)}


def first_passage_success(path_values, target) -> bool:
    """Return whether any value reached target before the supplied deadline samples ended."""
    return any(float(value) >= float(target) for value in path_values)


def simulate_wealth(*, initial, annual_return, annual_volatility, months=None, monthly_contribution=0, paths=1000, seed=7, horizon_days=None, target=None) -> dict:
    if initial <= 0 or paths <= 0 or annual_volatility < 0 or (months is not None and months <= 0) or (horizon_days is not None and horizon_days <= 0):
        raise ValueError("initial/horizon/paths must be positive and volatility non-negative")
    daily = horizon_days is not None
    steps = int(horizon_days if daily else months)
    rng = random.Random(seed)
    period_mu = float(annual_return) / (365 if daily else 12)
    period_sigma = float(annual_volatility) / ((365 if daily else 12) ** 0.5)
    terminal = []
    hit_target = []
    for _ in range(paths):
        value = float(initial)
        hit = float(initial) >= float(target) if target is not None else False
        for _ in range(steps):
            value *= 1 + rng.gauss(period_mu, period_sigma)
            value += float(monthly_contribution) / (30 if daily else 1)
            value = max(0.0, value)
            if target is not None and value >= float(target):
                hit = True
        terminal.append(value)
        hit_target.append(hit)
    return {"months": months, "horizonDays": horizon_days, "paths": paths, "seed": seed, "simulationGranularity": "daily" if daily else "monthly", "quantiles": _quantiles(terminal), "terminalValues": terminal, "hitTarget": hit_target}


def goal_probability(*, initial, target, annual_return, annual_volatility, months=None, monthly_contribution=0, paths=1000, seed=7, as_of=None, target_date=None) -> dict:
    horizon_days = None
    if as_of is not None and target_date is not None:
        horizon_days = (date.fromisoformat(str(target_date)[:10]) - date.fromisoformat(str(as_of)[:10])).days
        if horizon_days <= 0:
            return {"horizonDays": max(0, horizon_days), "paths": paths, "seed": seed, "simulationGranularity": "daily", "quantiles": {}, "target": float(target), "probability": 0.0, "probabilityDefinition": "hit_by_deadline"}
    simulation = simulate_wealth(initial=initial, annual_return=annual_return, annual_volatility=annual_volatility, months=months, horizon_days=horizon_days, monthly_contribution=monthly_contribution, paths=paths, seed=seed, target=target)
    successes = sum(simulation["hitTarget"])
    simulation["target"] = float(target)
    simulation["probability"] = round(successes / paths, 4)
    simulation["probabilityDefinition"] = "hit_by_deadline"
    simulation.pop("terminalValues")
    simulation.pop("hitTarget")
    return simulation
