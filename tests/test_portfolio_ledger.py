from datetime import date
from decimal import Decimal
import unittest
from uuid import uuid4

from portfolio_ledger import PortfolioLedger, PortfolioLedgerConflict, PortfolioLedgerError
from transaction_schema import Action, Transaction


def tx(action, *, quantity="1", currency="TWD", symbol="2330", asset_type="台股", price=None, reversal_of=None):
    return Transaction(
        transaction_id=str(uuid4()),
        source_row_id="form:2",
        submitted_at="2026-08-04T12:00:00+08:00",
        submitter_email="owner@example.com",
        approved=True,
        transaction_date=date(2026, 8, 4),
        asset_type=asset_type,
        symbol=symbol,
        action=action,
        quantity=Decimal(quantity),
        unit="SHARE" if action in {Action.BUY, Action.SELL, Action.SPLIT} else "TWD",
        currency=currency,
        price=Decimal(price) if price is not None else None,
        reversal_of=reversal_of,
    )


class PortfolioLedgerTests(unittest.TestCase):
    def test_buy_sell_and_cash_reconcile(self):
        buy = tx(Action.BUY, quantity="2", price="100")
        sell = tx(Action.SELL, quantity="1", price="120")
        ledger = PortfolioLedger([tx(Action.DEPOSIT, quantity="1000", symbol="TWD"), buy, sell])
        self.assertEqual(ledger.state.position("台股", "2330"), Decimal("1"))
        self.assertEqual(ledger.state.cash_balance("TWD"), Decimal("920"))

    def test_replay_is_idempotent(self):
        deposit = tx(Action.DEPOSIT, quantity="100")
        ledger = PortfolioLedger([deposit])
        self.assertFalse(ledger.append(deposit))
        self.assertEqual(ledger.state.cash_balance("TWD"), Decimal("100"))

    def test_uuid_conflict_fails_closed(self):
        deposit = tx(Action.DEPOSIT, quantity="100")
        changed = Transaction(**{**deposit.__dict__, "quantity": Decimal("101")})
        ledger = PortfolioLedger([deposit])
        with self.assertRaises(PortfolioLedgerConflict):
            ledger.append(changed)

    def test_financing_is_separate_from_investment_cash(self):
        ledger = PortfolioLedger([tx(Action.BORROW, quantity="500")])
        self.assertEqual(ledger.state.cash_balance("TWD"), Decimal("500"))
        self.assertEqual(ledger.state.debt_balance("TWD"), Decimal("500"))

    def test_reversal_restores_original_state(self):
        deposit = tx(Action.DEPOSIT, quantity="100")
        ledger = PortfolioLedger([deposit])
        reversal = tx(Action.REVERSAL, quantity="0", symbol="TWD", reversal_of=deposit.transaction_id)
        ledger.append(reversal)
        self.assertEqual(ledger.state.cash_balance("TWD"), Decimal("0"))

    def test_split_and_transfer(self):
        buy = tx(Action.BUY, quantity="2", price="10")
        split = tx(Action.SPLIT, quantity="2", price=None)
        transfer = tx(Action.TRANSFER, quantity="1", symbol="2330->TSM", asset_type="台股")
        ledger = PortfolioLedger([tx(Action.DEPOSIT, quantity="100"), buy, split, transfer])
        self.assertEqual(ledger.state.position("台股", "2330"), Decimal("3"))
        self.assertEqual(ledger.state.position("台股", "TSM"), Decimal("1"))

    def test_fx_conversion(self):
        ledger = PortfolioLedger([tx(Action.DEPOSIT, quantity="1000", currency="USD", symbol="USD"), tx(
            Action.FX_CONVERSION, quantity="100", currency="USD", symbol="USD/TWD", price="32"
        )])
        self.assertEqual(ledger.state.cash_balance("USD"), Decimal("900"))
        self.assertEqual(ledger.state.cash_balance("TWD"), Decimal("3200"))

    def test_insufficient_position_is_rejected(self):
        with self.assertRaises(PortfolioLedgerError):
            PortfolioLedger([tx(Action.SELL, quantity="1", price="10")])

    def test_set_balance_can_replace_position_or_debt(self):
        position = tx(Action.SET_BALANCE, quantity="2000", symbol="006208", asset_type="台股")
        debt = tx(Action.SET_BALANCE, quantity="1870000", symbol="Current_Debt", asset_type="質押負債")
        ledger = PortfolioLedger([position, debt])
        self.assertEqual(ledger.state.position("台股", "006208"), Decimal("2000"))
        self.assertEqual(ledger.state.debt_balance("TWD"), Decimal("1870000"))

    def test_set_pledge_rate_is_metadata_not_cash_or_position(self):
        rate = tx(Action.SET_PLEDGE_RATE, quantity="2.25", symbol="Rate", asset_type="質押利率")
        ledger = PortfolioLedger([rate])
        self.assertEqual(ledger.state.cash, {})
        self.assertEqual(ledger.state.positions, {})
        self.assertEqual(ledger.state.debt, {})


if __name__ == "__main__":
    unittest.main()
