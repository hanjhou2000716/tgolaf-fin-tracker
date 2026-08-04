"""Seeded Monte Carlo wealth and goal probability estimates."""

import random


def _quantiles(values):
    values = sorted(values)
    def q(percent):
        index = (len(values) - 1) * percent
        low, high = int(index), min(len(values) - 1, int(index) + 1)
        return values[low] + (values[high] - values[low]) * (index - low)
    return {"P5": round(q(.05), 2), "P25": round(q(.25), 2), "P50": round(q(.50), 2), "P75": round(q(.75), 2), "P95": round(q(.95), 2)}


def simulate_wealth(*, initial, annual_return, annual_volatility, months, monthly_contribution=0, paths=1000, seed=7) -> dict:
    if initial <= 0 or months <= 0 or paths <= 0 or annual_volatility < 0:
        raise ValueError("initial/months/paths must be positive and volatility non-negative")
    rng = random.Random(seed)
    monthly_mu = float(annual_return) / 12
    monthly_sigma = float(annual_volatility) / (12 ** 0.5)
    terminal = []
    for _ in range(paths):
        value = float(initial)
        for _ in range(months):
            value *= 1 + rng.gauss(monthly_mu, monthly_sigma)
            value += float(monthly_contribution)
            value = max(0.0, value)
        terminal.append(value)
    return {"months": months, "paths": paths, "seed": seed, "quantiles": _quantiles(terminal), "terminalValues": terminal}


def goal_probability(*, initial, target, annual_return, annual_volatility, months, monthly_contribution=0, paths=1000, seed=7) -> dict:
    simulation = simulate_wealth(initial=initial, annual_return=annual_return, annual_volatility=annual_volatility, months=months, monthly_contribution=monthly_contribution, paths=paths, seed=seed)
    successes = sum(value >= float(target) for value in simulation["terminalValues"])
    simulation["target"] = float(target)
    simulation["probability"] = round(successes / paths, 4)
    simulation.pop("terminalValues")
    return simulation
