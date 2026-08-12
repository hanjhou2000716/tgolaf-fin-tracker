import unittest

from goal_contract import GOALS, evaluate_goal_ladder, fx_quote_is_qualified, initial_goal_state


class GoalContractTests(unittest.TestCase):
    def test_canonical_ladder(self):
        self.assertEqual([goal["id"] for goal in GOALS], ["G1_TWD_10M", "G2_USD_1M", "G3_TWD_100M"])
        self.assertEqual(GOALS[0]["targetDate"], "2028-07-16")
        self.assertEqual(GOALS[1]["targetCurrency"], "USD")

    def test_goal_crossing_is_monotonic_and_supports_multiple_goals(self):
        fx = {"rate": 32.0, "quality": "fresh", "is_stale": False, "fallback_used": False}
        state = evaluate_goal_ladder(state=initial_goal_state(), net_asset_twd=35_000_000, as_of="2026-08-12", fx_quote=fx)
        self.assertEqual([item["goalId"] for item in state["achievements"]], ["G1_TWD_10M", "G2_USD_1M"])
        self.assertEqual(state["activeGoalId"], "G3_TWD_100M")
        again = evaluate_goal_ladder(state=state, net_asset_twd=5_000_000, as_of="2026-08-13", fx_quote=fx)
        self.assertEqual([item["goalId"] for item in again["achievements"]], ["G1_TWD_10M", "G2_USD_1M"])
        self.assertEqual(again["activeGoalId"], "G3_TWD_100M")

    def test_unqualified_fx_cannot_persist_usd_achievement(self):
        fallback = {"rate": 32.0, "quality": "fallback", "is_stale": True, "fallback_used": True}
        state = evaluate_goal_ladder(state=initial_goal_state(), net_asset_twd=40_000_000, as_of="2026-08-12", fx_quote=fallback)
        self.assertEqual([item["goalId"] for item in state["achievements"]], ["G1_TWD_10M"])
        self.assertEqual(state["activeGoalId"], "G2_USD_1M")
        self.assertFalse(fx_quote_is_qualified(fallback))

    def test_completed_state_is_terminal(self):
        fx = {"rate": 32.0, "quality": "fresh", "is_stale": False, "fallback_used": False}
        state = evaluate_goal_ladder(state=initial_goal_state(), net_asset_twd=110_000_000, as_of="2026-08-12", fx_quote=fx)
        self.assertEqual(state["status"], "completed")
        self.assertIsNone(state["activeGoalId"])
        later = evaluate_goal_ladder(state=state, net_asset_twd=1, as_of="2026-08-13", fx_quote=fx)
        self.assertEqual(later["status"], "completed")
        self.assertEqual(later["completedAt"], state["completedAt"])


if __name__ == "__main__":
    unittest.main()
