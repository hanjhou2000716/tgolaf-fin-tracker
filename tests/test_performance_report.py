from datetime import date
import unittest

from performance_report import MetricScope, build_performance_report, compare_benchmarks


class PerformanceReportTests(unittest.TestCase):
    def test_benchmark_comparison_has_same_span(self):
        result = compare_benchmarks([100, 110, 121], {"006208": [100, 105, 115]})
        self.assertAlmostEqual(result["portfolio"]["twr"], 0.21)
        self.assertAlmostEqual(result["006208"]["twr"], 0.15)

    def test_mismatched_benchmark_is_rejected(self):
        with self.assertRaises(ValueError):
            compare_benchmarks([100, 110], {"QQQ": [100]})

    def test_report_keeps_metric_scope(self):
        report = build_performance_report([100, 105, 103], cash_flows=[(date(2026, 1, 1), -100), (date(2026, 1, 3), 103)], scope=MetricScope(includes_debt=False))
        self.assertIn("xirr", report)
        self.assertFalse(report["scope"]["includesDebt"])
        self.assertIn("portfolio", report["benchmarks"])


if __name__ == "__main__":
    unittest.main()
