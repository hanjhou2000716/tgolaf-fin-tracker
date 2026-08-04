import unittest

from scenario_lab import run_scenario


class ScenarioLabTests(unittest.TestCase):
    def test_shocks_reduce_net_asset_and_maintenance(self):
        result = run_scenario(
            total_asset=1000,
            net_asset=900,
            total_debt=100,
            pledged_value=300,
            tw_value=600,
            us_value=400,
            tw_shock=-0.1,
        )
        self.assertLess(result["netAsset"], 900)
        self.assertLess(result["maintenanceRatio"], 300)
        self.assertLess(result["drawdown"], 0)

    def test_overlapping_nvidia_value_is_not_double_counted(self):
        result = run_scenario(
            total_asset=1000,
            net_asset=1000,
            total_debt=0,
            pledged_value=0,
            tw_value=0,
            us_value=1000,
            nvda_value=100,
            nvda_shock=-0.2,
        )
        self.assertEqual(result["asset"], 980)

    def test_interest_shock_creates_topup_need(self):
        result = run_scenario(
            total_asset=1000,
            net_asset=800,
            total_debt=500,
            pledged_value=900,
            interest_rate_shock=0.2,
        )
        self.assertGreater(result["topUpRequired"], 0)


if __name__ == "__main__":
    unittest.main()
