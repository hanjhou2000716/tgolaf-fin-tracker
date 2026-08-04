import unittest
from datetime import date
from decimal import Decimal

from performance import performance_breakdown
from transaction_schema import Action, Transaction


def tx(action, amount):
    return Transaction(
        transaction_id=f"66666666-6666-4666-8666-{amount:012d}",
        source_row_id="Form:2",
        submitted_at="2026-08-04T00:00:00Z",
        submitter_email="owner@example.com",
        approved=True,
        transaction_date=date(2026, 8, 4),
        asset_type="CASH",
        symbol="TWD",
        action=action,
        quantity=Decimal(amount),
        unit="TWD",
        currency="TWD",
    )


class PerformanceTests(unittest.TestCase):
    def test_deposit_is_not_market_profit(self):
        result = performance_breakdown(110, 0, [tx(Action.DEPOSIT, 100)])
        self.assertEqual(result["externalCashFlow"], 100.0)
        self.assertEqual(result["marketPnl"], 10.0)
        self.assertTrue(result["reconciled"])

    def test_borrowing_is_financing_flow(self):
        result = performance_breakdown(100, 0, [tx(Action.BORROW, 100)])
        self.assertEqual(result["financingCashFlow"], 100.0)
        self.assertEqual(result["marketPnl"], 0.0)

    def test_interest_and_fee_are_explicit(self):
        result = performance_breakdown(103, 100, [tx(Action.INTEREST, 5), tx(Action.FEE, 2)])
        self.assertEqual(result["income"], 5.0)
        self.assertEqual(result["expenses"], -2.0)
        self.assertEqual(result["marketPnl"], 0.0)


if __name__ == "__main__":
    unittest.main()
