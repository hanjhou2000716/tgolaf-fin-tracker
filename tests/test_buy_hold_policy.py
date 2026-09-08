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
    buy_hold_telegram_emoji,
    build_settlement_telegram_message,
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

    def test_telegram_line_maps_locked_copy_without_private_values(self):
        cases = [
            ("BLUE", "🔵 藍燈｜正常持有"),
            ("GREEN", "🟢 綠燈｜原型買進區（本月可買 006208）"),
            ("YELLOW", "🟡 黃燈｜初階加碼區（2% NAV）"),
            ("ORANGE", "🟠 橘燈｜深度加碼區（3% NAV）"),
            ("RED", "🔴 紅燈｜極端加碼區（5% NAV）"),
        ]
        emoji_names = {
            "BLUE": ("🔵", "藍燈", "正常持有"),
            "GREEN": ("🟢", "綠燈", "原型買進區"),
            "YELLOW": ("🟡", "黃燈", "初階加碼區"),
            "ORANGE": ("🟠", "橘燈", "深度加碼區"),
            "RED": ("🔴", "紅燈", "極端加碼區"),
        }
        for code, expected in cases:
            emoji, name, meaning = emoji_names[code]
            with self.subTest(code=code):
                line = buy_hold_telegram_line({"light": {"code": code, "emoji": emoji, "name": name, "meaning": meaning}})
                self.assertEqual(line, f"🚦 Buy&Hold：{expected}")
                self.assertNotIn("100000", line)

    def test_telegram_line_unavailable_is_safe(self):
        self.assertEqual(
            buy_hold_telegram_line({"light": {"code": "UNAVAILABLE"}}),
            "🚦 Buy&Hold：⚪ 資料暫不可用",
        )

    def test_settlement_message_prefixes_the_current_light_and_stays_two_lines(self):
        cases = [
            ("BLUE", "🔵", -8966, -0.1, "可憐的阿洲，今天賠了 8,966 元 (-0.1%)"),
            ("GREEN", "🟢", 1234, 0.7, "厲害的阿洲，今天賺了 1,234 元 (+0.7%)"),
            ("YELLOW", "🟡", 0, -0.0, "阿洲今天持平，損益 0 元 (+0.0%)"),
            ("ORANGE", "🟠", 50.5, 0.2, "厲害的阿洲，今天賺了 50 元 (+0.2%)"),
            ("RED", "🔴", -1, -0.01, "可憐的阿洲，今天賠了 1 元 (-0.0%)"),
        ]
        for code, emoji, difference, percentage, expected_body in cases:
            with self.subTest(code=code):
                message = build_settlement_telegram_message(
                    "09/09",
                    difference,
                    percentage,
                    {"light": {"code": code, "emoji": "⚪"}},
                )
                self.assertEqual(message, f"✅ 09/09 結算完畢！\n{emoji} {expected_body}")
                self.assertEqual(message.count("\n"), 1)
                self.assertNotIn("Buy&Hold", message)

    def test_settlement_message_uses_white_light_for_missing_or_unknown_signal(self):
        for policy in ({}, {"light": {"code": "UNAVAILABLE"}}, {"light": {"code": "unknown"}}):
            with self.subTest(policy=policy):
                self.assertEqual(buy_hold_telegram_emoji(policy), "⚪")
                message = build_settlement_telegram_message("09/09", -8966, -0.1, policy)
                self.assertEqual(
                    message,
                    "✅ 09/09 結算完畢！\n⚪ 可憐的阿洲，今天賠了 8,966 元 (-0.1%)",
                )


if __name__ == "__main__":
    unittest.main()
