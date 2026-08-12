import unittest

from goal_state import build_goal_forecast


class GoalStateTests(unittest.TestCase):
    def test_overdue_goal_is_zero_probability(self):
        state = {"activeGoalId": "G1_TWD_10M", "achievements": [], "status": "active"}
        result = build_goal_forecast(state=state, net_asset_twd=1_000_000, annual_return=.06, annual_volatility=.15, as_of="2028-07-17", paths=2000)
        self.assertTrue(result["overdue"])
        self.assertEqual(result["probability"], 0.0)
        self.assertEqual(result["probabilityDefinition"], "hit_by_deadline")

    def test_usd_goal_requires_qualified_spot_quote(self):
        state = {"activeGoalId": "G2_USD_1M", "achievements": [], "status": "active"}
        result = build_goal_forecast(state=state, net_asset_twd=30_000_000, annual_return=.06, annual_volatility=.15, as_of="2026-08-12", fx_quote={"rate": 32, "quality": "fallback", "fallback_used": True, "is_stale": True}, paths=2000)
        self.assertIsNone(result["probability"])
        self.assertTrue(result["fx"]["required"])
        self.assertTrue(result["fx"]["fallbackUsed"])


if __name__ == "__main__":
    unittest.main()
