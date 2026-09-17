import math
import unittest

from risk import (
    HALF_KELLY_LIMIT,
    beta_capacity,
    beta_status,
    calculate_nav_beta,
    build_quarterly_kelly_candidate,
    estimate_beta_from_returns,
    maintenance_status,
    quarterly_half_kelly,
)


class NavBetaTests(unittest.TestCase):
    def test_unlevered_006208_is_one(self):
        result = calculate_nav_beta({"006208": 1_000_000}, 1_000_000, 0, {"006208": 1.0}, market_by_symbol={"006208": "tw"})
        self.assertEqual(result["status"], "READY")
        self.assertAlmostEqual(result["navBeta"], 1.0)
        self.assertAlmostEqual(result["grossLeverage"], 1.0)

    def test_borrow_and_buy_006208_is_one_point_five(self):
        result = calculate_nav_beta({"006208": 1_500_000}, 1_500_000, 500_000, {"006208": 1.0}, market_by_symbol={"006208": "tw"})
        self.assertAlmostEqual(result["navBeta"], 1.5)
        self.assertAlmostEqual(result["debtToNav"], 0.5)

    def test_borrow_and_buy_00685l_is_two(self):
        result = calculate_nav_beta({"006208": 1_000_000, "00685L": 500_000}, 1_500_000, 500_000, {"006208": 1.0, "00685L": 2.0}, market_by_symbol={"006208": "tw", "00685L": "tw"})
        self.assertAlmostEqual(result["navBeta"], 2.0)
        self.assertAlmostEqual(sum(result["contributions"].values()), 2.0)

    def test_cash_has_zero_beta_and_collateral_is_not_a_second_position(self):
        result = calculate_nav_beta({"006208": 1_000_000, "CASH_TWD": 500_000}, 1_500_000, 0, {"006208": 1.0, "CASH_TWD": 0.0}, market_by_symbol={"006208": "tw"})
        self.assertAlmostEqual(result["betaExposureTwd"], 1_000_000)
        self.assertAlmostEqual(result["navBeta"], 2 / 3)

    def test_unknown_material_holding_fails_closed(self):
        result = calculate_nav_beta({"006208": 900_000, "UNKNOWN": 100_000}, 1_000_000, 0, {"006208": 1.0})
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertTrue(result["missing"][0]["material"])

    def test_invalid_balance_and_nonpositive_nav_fail_closed(self):
        self.assertEqual(calculate_nav_beta({}, 100, 100, {})["status"], "UNAVAILABLE")
        self.assertEqual(calculate_nav_beta({}, float("nan"), 0, {})["status"], "UNAVAILABLE")

    def test_estimator_requires_paired_history(self):
        self.assertEqual(estimate_beta_from_returns([1, 2], [1, 2])["status"], "UNAVAILABLE")
        asset = [i / 100 for i in range(104)]
        benchmark = [i / 200 for i in range(104)]
        result = estimate_beta_from_returns(asset, benchmark)
        self.assertEqual(result["status"], "READY")
        self.assertTrue(math.isfinite(result["beta"]))

    def test_quarterly_kelly_is_conservative_and_invalid_is_unavailable(self):
        result = quarterly_half_kelly(0.20, 0.10)
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertAlmostEqual(result["mu"], 0.08)
        self.assertAlmostEqual(result["sigma"], 0.18)
        self.assertLessEqual(result["halfKellyLimit"], HALF_KELLY_LIMIT)
        self.assertEqual(quarterly_half_kelly(0, 0.18)["status"], "UNAVAILABLE")

    def test_quarterly_candidate_excludes_future_prices(self):
        prices = [{"date": f"2020-{index:02d}", "close": 100 + index} for index in range(1, 270)]
        prices.append({"date": "2099-01", "close": 999999})
        result = build_quarterly_kelly_candidate(prices, data_cutoff="2020-270")
        self.assertNotEqual(result.get("seriesEnd"), "2099-01")
        self.assertLess(result["observations"], 270)

    def test_threshold_boundaries(self):
        self.assertEqual(beta_status(114.999)[1], "risk-watch")
        self.assertEqual(beta_status(115)[1], "risk-alert")
        self.assertEqual(maintenance_status(1, 190)[1], "risk-good")
        self.assertEqual(maintenance_status(1, 167)[1], "risk-watch")
        self.assertEqual(maintenance_status(1, 150)[1], "risk-orange")
        self.assertEqual(maintenance_status(1, 130)[1], "risk-alert")


if __name__ == "__main__":
    unittest.main()
