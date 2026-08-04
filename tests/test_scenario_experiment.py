import unittest

from scenario_experiment import run_adjustable_scenario


PORTFOLIO = {"total_asset": 1000, "net_asset": 800, "total_debt": 200, "pledged_value": 400, "tw_value": 700, "us_value": 300, "nvda_value": 50, "tsmc_value": 100}


class ScenarioExperimentTests(unittest.TestCase):
    def test_returns_baseline_and_shocked_comparison(self):
        result = run_adjustable_scenario(portfolio=PORTFOLIO, shocks={"tw": -0.1, "us": -0.2})
        self.assertLess(result["netAsset"], result["baseline"]["netAsset"])
        self.assertIn("maintenanceAbove150", result["guardrails"])

    def test_unknown_or_extreme_shock_is_rejected(self):
        with self.assertRaises(ValueError):
            run_adjustable_scenario(portfolio=PORTFOLIO, shocks={"oil": -0.1})
        with self.assertRaises(ValueError):
            run_adjustable_scenario(portfolio=PORTFOLIO, shocks={"tw": -1.1})


if __name__ == "__main__":
    unittest.main()
