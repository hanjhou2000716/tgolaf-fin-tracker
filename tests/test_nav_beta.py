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
    resolve_beta_policy,
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

    def test_fixed_policy_cannot_be_overridden(self):
        result = calculate_nav_beta({"006208": 1_000_000, "00685L": 500_000}, 1_500_000, 500_000, {"006208": 9.0, "00685L": 0.1})
        self.assertAlmostEqual(result["navBeta"], 2.0)

    def test_resolve_policy_accepts_only_finite_non_fixed_values(self):
        policy = resolve_beta_policy({"QQQ": 0.8, "006208": 9, "CASH_TWD": 4, "BAD": "NaN"})
        self.assertEqual(policy["006208"], 1.0)
        self.assertEqual(policy["00685L"], 2.0)
        self.assertEqual(policy["QQQ"], 0.8)
        self.assertNotIn("CASH_TWD", policy)
        self.assertNotIn("BAD", policy)

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
        from datetime import date, timedelta

        prices = [{"date": (date(2020, 1, 3) + timedelta(days=index * 7)).isoformat(), "close": 100 + index}
                  for index in range(269)]
        prices.append({"date": "2099-01-02", "close": 999999})
        result = build_quarterly_kelly_candidate(prices, data_cutoff=prices[-2]["date"])
        self.assertNotEqual(result.get("seriesEnd"), "2099-01-02")
        self.assertEqual(result["observations"], 269)

    def test_kelly_cagr_uses_actual_elapsed_calendar_period(self):
        from datetime import date, timedelta

        rows = [{"date": (date(2021, 1, 1) + timedelta(days=index * 7)).isoformat(), "close": 100 + index}
                for index in range(261)]
        result = build_quarterly_kelly_candidate(rows, data_cutoff=rows[-1]["date"])
        years = (date.fromisoformat(rows[-1]["date"]) - date.fromisoformat(rows[0]["date"])).days / 365.2425
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertAlmostEqual(result["mu"], min(0.08, (360 / 100) ** (1 / years) - 1), places=10)

    def test_kelly_accepts_258_complete_five_year_weekly_prices(self):
        from datetime import date, timedelta

        first = date(2021, 9, 24)
        rows = []
        for index in range(261):
            session = first + timedelta(days=index * 7)
            if index in {40, 120, 200}:
                continue  # full-week exchange closures, not missing weekly returns
            rows.append({"date": session.isoformat(), "close": 100 + index * 0.35 + index % 5})
        self.assertEqual(len(rows), 258)
        result = build_quarterly_kelly_candidate(rows, data_cutoff="2026-09-30")
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(result["observations"], 258)
        self.assertGreaterEqual(result["volatilityObservations"], 104)
        self.assertEqual(result["seriesEnd"], rows[-1]["date"])

    def test_kelly_rejects_duplicate_or_short_weekly_history(self):
        from datetime import date, timedelta

        start = date(2023, 1, 6)
        short = [{"date": (start + timedelta(days=index * 7)).isoformat(), "close": 100 + index} for index in range(160)]
        self.assertEqual(build_quarterly_kelly_candidate(short)["status"], "INSUFFICIENT_EVIDENCE")
        long = [{"date": (date(2021, 9, 24) + timedelta(days=index * 7)).isoformat(), "close": 100 + index} for index in range(261)]
        long.append(dict(long[100]))
        self.assertIn("duplicate", build_quarterly_kelly_candidate(long)["reason"])

    def test_kelly_empty_or_nonpositive_research_prices_fail_closed(self):
        self.assertEqual(build_quarterly_kelly_candidate([])["status"], "INSUFFICIENT_EVIDENCE")
        bad = [{"date": "2021-01-01", "close": 100}, {"date": "2021-01-08", "close": float("nan")}]
        self.assertIn("finite and positive", build_quarterly_kelly_candidate(bad)["reason"])

    def test_threshold_boundaries(self):
        self.assertEqual(beta_status(114.999)[1], "risk-watch")
        self.assertEqual(beta_status(115)[1], "risk-alert")
        self.assertEqual(maintenance_status(1, 190)[1], "risk-good")
        self.assertEqual(maintenance_status(1, 167)[1], "risk-watch")
        self.assertEqual(maintenance_status(1, 150)[1], "risk-orange")
        self.assertEqual(maintenance_status(1, 130)[1], "risk-alert")


if __name__ == "__main__":
    unittest.main()
