import unittest
from pathlib import Path


class GoalContractStaticTests(unittest.TestCase):
    def test_runtime_has_no_legacy_fixed_goal_probability(self):
        source = (Path(__file__).resolve().parents[1] / "runtime_extensions.py").read_text(encoding="utf-8")
        self.assertNotIn("months=60", source)
        self.assertNotIn("paths=250", source)
        self.assertNotIn("max(float(net_asset), 10_000_000)", source)

    def test_pipeline_legacy_progress_uses_backend_target(self):
        source = (Path(__file__).resolve().parents[1] / "dashboard_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("active_goal_meta", source)
        self.assertIn("targetTwdEquivalent", source)
        self.assertNotIn("net_asset / 10000000", source)
        self.assertNotIn("10,000,000 TWD", source)

    def test_pledge_principal_is_separate_from_risk_liability(self):
        source = (Path(__file__).resolve().parents[1] / "dashboard_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("debt_principal", source)
        self.assertIn('"principal": round(debt_principal, 2)', source)
        self.assertIn('"pledgePrincipal": round(debt_principal, 2)', source)
        self.assertIn("pledged_loan_value = debt_principal", source)
        self.assertIn("total_debt = debt + accumulated_interest", source)


if __name__ == "__main__":
    unittest.main()
