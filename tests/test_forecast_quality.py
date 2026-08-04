import unittest

from forecast_quality import calibrate_quantiles, margin_call_probability


class ForecastQualityTests(unittest.TestCase):
    def test_margin_probability_is_bounded(self):
        result = margin_call_probability(current_ratio=180, daily_volatility=.02, horizon_days=20)
        self.assertGreaterEqual(result["probability"], 0)
        self.assertLessEqual(result["probability"], 1)

    def test_quantile_calibration_returns_coverage_and_loss(self):
        result = calibrate_quantiles({"P5": 90, "P25": 95, "P50": 100, "P75": 105, "P95": 110}, [90, 100, 110])
        self.assertIn("coverage", result["P50"])
        self.assertIn("pinballLoss", result["P95"])


if __name__ == "__main__":
    unittest.main()
