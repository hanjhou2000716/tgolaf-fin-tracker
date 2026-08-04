import unittest

from risk_center import build_pledge_risk_center


class RiskCenterTests(unittest.TestCase):
    def test_discount_and_stress_report_guardrails(self):
        report = build_pledge_risk_center(collateral={"006208": 1000, "2330": 500}, debt=800, discounts={"006208": 0.1}, stress_decline=0.2)
        self.assertEqual(report["currentRatio"], 175.0)
        self.assertEqual(report["status"], "WARNING")
        self.assertFalse(report["guardrails"]["stressAboveWarning"])
        self.assertFalse(report["guardrails"]["leverageIncreaseAllowed"])

    def test_no_debt_is_safe_but_never_authorizes_leverage(self):
        report = build_pledge_risk_center(collateral=1000, debt=0)
        self.assertFalse(report["guardrails"]["leverageIncreaseAllowed"])


if __name__ == "__main__":
    unittest.main()
