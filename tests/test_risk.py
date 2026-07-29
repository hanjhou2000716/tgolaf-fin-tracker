import unittest

from risk import (
    HALF_KELLY_LIMIT,
    beta_capacity,
    beta_status,
    maintenance_ratio,
    maintenance_status,
    stress_scenarios,
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
        self.assertEqual(maintenance_status(100, 190), ("🟢 可加槓桿", "risk-good"))

    def test_stress_scenarios_reduce_net_asset_and_collateral(self):
        scenarios = stress_scenarios(1_000, 10_000, 5_000, 4_000, 2_000)
        self.assertEqual(scenarios[0]["netImpact"], -100)
        self.assertEqual(scenarios[1]["netAsset"], 9_800)
        self.assertEqual(scenarios[1]["maintenance"], 210)


if __name__ == "__main__":
    unittest.main()
