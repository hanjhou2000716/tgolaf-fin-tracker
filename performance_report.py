"""Performance report contract with benchmark comparison and scope flags."""

from dataclasses import dataclass
from typing import Mapping, Sequence

from metrics import summarize_performance, time_weighted_return, xirr


@dataclass(frozen=True)
class MetricScope:
    includes_cash: bool = True
    includes_debt: bool = True
    includes_fx: bool = True
    includes_fees: bool = True

    def as_dict(self) -> dict:
        return {
            "includesCash": self.includes_cash,
            "includesDebt": self.includes_debt,
            "includesFx": self.includes_fx,
            "includesFees": self.includes_fees,
        }


def compare_benchmarks(portfolio_values: Sequence[float], benchmarks: Mapping[str, Sequence[float]], *, periods_per_year=252) -> dict:
    """Return portfolio metrics and comparable TWR for each benchmark.

    Benchmark series must cover the same observation dates as the portfolio;
    unequal lengths fail closed instead of silently comparing different spans.
    """
    values = list(portfolio_values)
    if len(values) < 2:
        raise ValueError("portfolio_values requires at least two observations")
    result = {"portfolio": summarize_performance(values, periods_per_year=periods_per_year)}
    for name, series in benchmarks.items():
        series = list(series)
        if len(series) != len(values):
            raise ValueError(f"benchmark {name} length does not match portfolio")
        result[str(name)] = {
            "twr": round(time_weighted_return(series), 8),
            "observations": len(series),
        }
    return result


def build_performance_report(values: Sequence[float], *, cash_flows=None, benchmarks=None, scope=None, periods_per_year=252) -> dict:
    values = list(values)
    report = summarize_performance(values, periods_per_year=periods_per_year)
    if cash_flows is not None:
        report["xirr"] = round(xirr(cash_flows), 8) if len(cash_flows) >= 2 else 0.0
    report["scope"] = (scope or MetricScope()).as_dict()
    report["benchmarks"] = compare_benchmarks(values, benchmarks or {}, periods_per_year=periods_per_year)
    return report
