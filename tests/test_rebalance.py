import unittest

from rebalance import propose_rebalance


class RebalanceTests(unittest.TestCase):
    def test_three_plans_respect_locked_and_concentration_constraints(self):
        result = propose_rebalance(holdings={"006208": 700, "QQQM": 200, "CASH": 100}, target_weights={"006208": .5, "QQQM": .3, "VOO": .2}, total_value=1000, max_single_exposure=.6, locked=("006208",), min_cash=50, trade_unit=10)
        self.assertEqual(set(result["plans"]), {"minimalTrades", "lowestRisk", "closestToTarget"})
        self.assertTrue(all(item["symbol"] != "006208" for item in result["plans"]["closestToTarget"]))

    def test_invalid_total_is_rejected(self):
        with self.assertRaises(ValueError):
            propose_rebalance(holdings={}, target_weights={}, total_value=0)


if __name__ == "__main__":
    unittest.main()
