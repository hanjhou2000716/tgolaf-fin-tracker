import unittest

from risk import (
    HALF_KELLY_LIMIT,
    beta_capacity,
    beta_status,
    maintenance_ratio,
    maintenance_status,
    stress_scenarios,
    composite_guardrails,
)


class RiskFormulaTests(unittest.TestCase):
    def test_beta_capacity_and_thresholds(self):
        self.assertAlmostEqual(beta_capacity(HALF_KELLY_LIMIT), 100)
        self.assertEqual(beta_status(115), ("🟡 Beta維持", "risk-watch"))
        self.assertEqual(beta_status(115.01), ("🔴 加原型補現金", "risk-alert"))

    def test_maintenance_thresholds(self):
        self.assertEqual(maintenance_ratio(0, 0), 0)
        self.assertEqual(maintenance_status(100, 149.9), ("🔴 補擔保品", "risk-alert"))
        self.assertEqual(maintenance_status(100, 150), ("🟡 注意槓桿", "risk-watch"))
        self.assertEqual(maintenance_status(100, 190), ("🟢 維持率充足", "risk-good"))

    def test_stress_scenarios_reduce_net_asset_and_collateral(self):
        scenarios = stress_scenarios(1_000, 10_000, 5_000, 4_000, 2_000)
        self.assertEqual(scenarios[0]["netImpact"], -100)
        self.assertEqual(scenarios[1]["netAsset"], 9_800)
        self.assertEqual(scenarios[1]["maintenance"], 210)

    def test_beta_alone_cannot_authorize_more_risk(self):
        result = composite_guardrails(80, 210, 10, 20, data_fresh=True)
        self.assertTrue(result["eligible"])
        self.assertIn("政策允許", result["recommendation"])

    def test_failed_guardrail_blocks_risk_increase(self):
        result = composite_guardrails(80, 210, 40, 20, data_fresh=True)
        self.assertFalse(result["eligible"])
        self.assertIn("禁止", result["recommendation"])
        self.assertFalse(next(rule for rule in result["rules"] if rule["name"] == "concentration")["passed"])

    def test_stale_data_blocks_any_recommendation(self):
        result = composite_guardrails(80, 210, 10, 20, data_fresh=False)
        self.assertFalse(result["eligible"])


if __name__ == "__main__":
    unittest.main()
