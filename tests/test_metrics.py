import unittest
from datetime import date, timedelta

from metrics import max_drawdown, summarize_performance, time_weighted_return, xirr


class MetricsTests(unittest.TestCase):
    def test_twr_excludes_external_deposit(self):
        self.assertAlmostEqual(time_weighted_return([100, 210, 210], [0, 100, 0]), 0.1)

    def test_xirr_annualizes_simple_cash_flow(self):
        start = date(2025, 1, 1)
        end = date(2026, 1, 1)
        self.assertAlmostEqual(xirr([(start, -100), (end, 110)]), 0.1, places=4)

    def test_drawdown_and_recovery(self):
        drawdown, recovery_days = max_drawdown([100, 120, 90, 120])
        self.assertAlmostEqual(drawdown, -0.25)
        self.assertEqual(recovery_days, 1)

    def test_summary_contains_all_required_metrics(self):
        summary = summarize_performance([100, 105, 102, 110])
        self.assertEqual(
            set(summary),
            {"twr", "annualizedReturn", "annualizedVolatility", "sharpe", "sortino", "calmar", "maxDrawdown", "recoveryDays"},
        )
        self.assertGreater(summary["twr"], 0)


if __name__ == "__main__":
    unittest.main()
