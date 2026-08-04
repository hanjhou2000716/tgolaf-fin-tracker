import unittest

from attribution import build_pnl_attribution


class AttributionTests(unittest.TestCase):
    def test_components_reconcile_to_net_change(self):
        result = build_pnl_attribution(
            1000,
            1120,
            {"TW_Stock_Value": 600, "US_Stock_Value": 300},
            {"TW_Stock_Value": 650, "US_Stock_Value": 330},
            income=10,
            expenses=-5,
            financing_cash_flow=20,
            external_cash_flow=0,
        )
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["other"], 15)
        self.assertEqual(sum(value for key, value in result.items() if key not in {"netChange", "reconciled"}), result["netChange"])

    def test_external_deposit_is_not_other_profit(self):
        result = build_pnl_attribution(1000, 1100, {}, {}, external_cash_flow=100)
        self.assertEqual(result["externalCashFlow"], 100)
        self.assertEqual(result["other"], 0)
        self.assertTrue(result["reconciled"])


if __name__ == "__main__":
    unittest.main()
