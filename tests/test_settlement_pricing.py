import unittest
from datetime import date
from decimal import Decimal

from settlement_pricing import enrich_missing_trade_prices
from transaction_schema import Action, Transaction


def trade(action=Action.BUY, price=None):
    return Transaction(
        transaction_id="00000000-0000-0000-0000-000000000101",
        source_row_id="Simple Form:2",
        submitted_at="2026-08-15T14:45:00+08:00",
        submitter_email="owner@example.com",
        approved=True,
        transaction_date=date(2026, 8, 15),
        asset_type="台股",
        symbol="006208",
        action=action,
        quantity=Decimal("1000"),
        unit="SHARE",
        currency="TWD",
        price=price,
    )


class SettlementPricingTests(unittest.TestCase):
    def test_fresh_quote_sets_price_and_audit_marker(self):
        result = enrich_missing_trade_prices([trade()], lambda _: 55.3)
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(result.pending, ())
        self.assertEqual(result.accepted[0].price, Decimal("55.3"))
        self.assertTrue(result.accepted[0].compatibility_used.startswith("settlement_quote_estimate:"))

    def test_stale_quote_goes_to_pending(self):
        quote = type("Quote", (), {"price": 55.3, "is_stale": True, "fallback_used": False, "source": "cache"})()
        result = enrich_missing_trade_prices([trade()], lambda _: quote)
        self.assertEqual(result.accepted, ())
        self.assertEqual(len(result.pending), 1)
        self.assertIn("settlement_quote_pending", result.pending[0].compatibility_used)

    def test_quote_exception_goes_to_pending(self):
        result = enrich_missing_trade_prices([trade()], lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
        self.assertEqual(result.accepted, ())
        self.assertEqual(len(result.pending), 1)

    def test_existing_price_and_non_trade_are_unchanged(self):
        priced = trade(price=Decimal("50"))
        deposit = Transaction(**{**trade(Action.DEPOSIT).__dict__, "asset_type": "現金_TWD", "symbol": "TWD", "unit": "TWD"})
        result = enrich_missing_trade_prices([priced, deposit], lambda _: (_ for _ in ()).throw(RuntimeError("must not fetch")))
        self.assertEqual(result.accepted, (priced, deposit))
        self.assertEqual(result.pending, ())


if __name__ == "__main__":
    unittest.main()
