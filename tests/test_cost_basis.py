from decimal import Decimal
from datetime import date
import unittest
from uuid import uuid4

from cost_basis import CostBasisEngine, CostBasisError
from transaction_schema import Action, Transaction


def trade(action, symbol="2330", qty="1", price="100", currency="TWD", asset_type="台股"):
    return Transaction(
        transaction_id=str(uuid4()), source_row_id="row", submitted_at="2026-08-04T12:00:00+08:00",
        submitter_email="owner@example.com", approved=True, transaction_date=date(2026, 8, 4),
        asset_type=asset_type, symbol=symbol, action=action, quantity=Decimal(qty), unit="SHARE",
        currency=currency, price=Decimal(price),
    )


class CostBasisTests(unittest.TestCase):
    def test_taiwan_moving_average_and_realized_pnl(self):
        engine = CostBasisEngine()
        engine.apply_all([trade(Action.BUY, qty="10", price="100"), trade(Action.BUY, qty="10", price="120"), trade(Action.SELL, qty="5", price="150")])
        summary = engine.summary("2330", "台股", market_price=160)
        self.assertEqual(summary["quantity"], Decimal("15"))
        self.assertEqual(summary["cost"], Decimal("1650"))
        self.assertEqual(summary["realizedPnl"], Decimal("200"))
        self.assertEqual(summary["unrealizedPnl"], Decimal("750"))

    def test_us_fifo(self):
        engine = CostBasisEngine(us_method="FIFO")
        engine.apply_all([trade(Action.BUY, "NVDA", "2", "100", "USD", "美股"), trade(Action.BUY, "NVDA", "2", "140", "USD", "美股"), trade(Action.SELL, "NVDA", "3", "150", "USD", "美股")])
        self.assertEqual(engine.summary("NVDA", "美股")["realizedPnl"], Decimal("110"))

    def test_sell_cannot_exceed_position(self):
        with self.assertRaises(CostBasisError):
            CostBasisEngine().apply(trade(Action.SELL, qty="1"))


if __name__ == "__main__":
    unittest.main()
