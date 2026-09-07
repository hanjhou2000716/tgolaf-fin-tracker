from datetime import date, datetime
import unittest

from buy_hold_policy import (
    build_buy_hold_policy,
    build_episode_policy,
    calculate_taiex_dd240,
    classify_market_light,
    evaluate_portfolio_gate,
    next_light_details,
    buy_hold_telegram_line,
)


def _history(closes):
    start = date(2025, 1, 1)
    return [{"date": date.fromordinal(start.toordinal() + i), "close": value} for i, value in enumerate(closes)]


class BuyHoldPolicyTests(unittest.TestCase):
    def test_dd240_uses_trading_rows_and_excludes_open_session(self):
        rows = _history([100 + i for i in range(239)] + [200])
        result = calculate_taiex_dd240(rows, as_of=datetime(2026, 1, 1, 13, 0))
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["sessions"], 240)
        self.assertEqual(result["highest240"], 338)
        self.assertAlmostEqual(result["dd240"], 200 / 338 - 1)

    def test_locked_light_boundaries(self):
        cases = [
            (-0.0499, "BLUE"), (-0.05, "GREEN"), (-0.0799, "GREEN"),
            (-0.08, "YELLOW"), (-0.1199, "YELLOW"), (-0.12, "ORANGE"),
            (-0.1799, "ORANGE"), (-0.18, "RED"),
        ]
        for dd, expected in cases:
            with self.subTest(dd=dd):
                self.assertEqual(classify_market_light(dd), expected)

    def test_next_light_returns_price_and_distance(self):
        result = next_light_details("GREEN", 1000, 950)
        self.assertEqual(result["code"], "YELLOW")
        self.assertAlmostEqual(result["triggerPrice"], 920)
        self.assertAlmostEqual(result["distancePoints"], -30)

    def test_jump_tier_creates_cumulative_episode_events_and_base_nav(self):
        closes = [100] * 240 + [95, 83, 85]
        rows = _history(closes)
        nav = [{"date": row["date"], "net_asset": 10000} for row in rows]
        result = build_episode_policy(rows, nav_history=nav, as_of=date(2026, 1, 1))
        tiers = [event["tier"] for event in result["episode"]["events"]]
        self.assertEqual(tiers, ["YELLOW", "ORANGE"])
        self.assertEqual(result["episode"]["episodeBaseNav"], 10000)
        self.assertAlmostEqual(result["episode"]["maxBudgetPct"], 0.10)

    def test_reset_requires_three_sessions_at_minus_one_percent(self):
        rows = _history([100] * 240 + [95, 90, 100, 100, 100, 90])
        result = build_episode_policy(rows, net_asset=10000, as_of=date(2026, 1, 1))
        self.assertIsNotNone(result["episode"]["episodeId"])
        self.assertEqual(len(result["episode"]["events"]), 2)
        self.assertNotEqual(
            result["episode"]["events"][0]["episodeId"],
            result["episode"]["events"][1]["episodeId"],
        )

    def test_portfolio_gate_cash_floor_debt_pledge_and_cap(self):
        gate = evaluate_portfolio_gate(
            net_asset=100000, total_cash=4000, total_debt=35000,
            maintenance_ratio=160, current_00685l_value=5000,
            desired_budget=10000, target_00685l_pct=0.2,
        )
        self.assertAlmostEqual(gate["cash"]["floor"], 3000)
        self.assertIn(gate["status"], {"正二受限／正二受限", "正二受限／部分限制", "正二受限"})
        self.assertFalse(gate["autoBorrowing"])
        self.assertEqual(gate["00685L"]["allowed"], 0)

    def test_no_maintenance_data_fails_closed_for_00685l(self):
        gate = evaluate_portfolio_gate(
            net_asset=100000, total_cash=10000, total_debt=10000,
            maintenance_ratio=None, current_00685l_value=0,
            desired_budget=2000, target_00685l_pct=0.1,
        )
        self.assertEqual(gate["maintenance"]["state"], "DATA UNAVAILABLE")
        self.assertEqual(gate["00685L"]["allowed"], 0)
        self.assertFalse(gate["autoBorrowing"])

    def test_00685l_cap_redirects_remaining_budget_to_006208(self):
        gate = evaluate_portfolio_gate(
            net_asset=100000, total_cash=20000, total_debt=10000,
            maintenance_ratio=180, current_00685l_value=9500,
            desired_budget=10000, target_00685l_pct=0.30,
        )
        self.assertAlmostEqual(gate["00685L"]["cap"], 10000)
        self.assertAlmostEqual(gate["00685L"]["allowed"], 500)
        self.assertAlmostEqual(gate["allocation"]["redirectedTo006208"], 2500)
        self.assertIn("部分限制", gate["status"])

    def test_light_downgrade_requires_two_points_and_three_sessions(self):
        rows = _history([100] * 240 + [82, 85, 85, 85])
        result = build_episode_policy(rows, net_asset=100000, as_of=date(2026, 1, 1))
        self.assertEqual(result["light"]["rawLight"], "ORANGE")
        self.assertEqual(result["light"]["code"], "ORANGE")
        rows = _history([100] * 240 + [82, 85, 85, 87])
        result = build_episode_policy(rows, net_asset=100000, as_of=date(2026, 1, 1))
        self.assertEqual(result["light"]["code"], "ORANGE")

    def test_market_data_unavailable_fails_closed(self):
        result = build_buy_hold_policy([], net_asset=100000, total_cash=50000, total_debt=0)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["light"]["emoji"], "⚪")
        self.assertFalse(result["recommendation"]["monthlyDca006208Allowed"])

    def test_telegram_line_maps_light_without_private_values(self):
        policy = build_buy_hold_policy(_history([100] * 240 + [95]), net_asset=100000)
        line = buy_hold_telegram_line(policy)
        self.assertTrue(line.startswith("🚦 Buy&Hold："))
        self.assertIn("綠燈", line)
        self.assertNotIn("100000", line)


if __name__ == "__main__":
    unittest.main()
