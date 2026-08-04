import unittest

from regime import classify_regime


class RegimeTests(unittest.TestCase):
    def test_trend_and_features_are_explainable(self):
        result = classify_regime([100 + i * 2 for i in range(80)])
        self.assertEqual(result["trend"], "bull")
        self.assertIn("annualizedVolatility", result["features"])
        self.assertGreaterEqual(result["confidence"], 0.5)

    def test_invalid_series_fails_closed(self):
        with self.assertRaises(ValueError):
            classify_regime([100, 0, 102])


if __name__ == "__main__":
    unittest.main()
