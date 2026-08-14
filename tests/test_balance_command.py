import unittest
from datetime import date
from decimal import Decimal

from attribution import build_pnl_attribution
from performance import performance_breakdown
from portfolio_ledger import PortfolioLedger, PortfolioLedgerConflict, PortfolioLedgerError
from transaction_command import CommandStatus, CommandValidationError, apply_reconciliation_events, build_ingestion_contract, build_ingestion_status, command_from_transaction, parse_set_balance_command
from transaction_schema import Action, Transaction
from transaction_schema import parse_transaction_rows


def tx(action, *, quantity="0", currency="TWD", symbol="TWD", asset_type="現金_TWD", transaction_id="00000000-0000-0000-0000-000000000001"):
    return Transaction(transaction_id=transaction_id, source_row_id="form:2", submitted_at="2026-08-14T14:45:00+08:00", submitter_email="owner@example.com", approved=True, transaction_date=date(2026, 8, 14), asset_type=asset_type, symbol=symbol, action=action, quantity=Decimal(quantity), unit=currency, currency=currency)


class BalanceCommandTests(unittest.TestCase):
    def test_explicit_set_balance_is_applied(self):
        command = parse_set_balance_command({"command": "SET_BALANCE", "source_row_id": "Form:2", "asset_type": "現金_TWD", "symbol": "TWD", "currency": "TWD", "target_balance": "150000", "transaction_date": "2026-08-14"})
        self.assertEqual(command.transaction.action, Action.SET_BALANCE)
        self.assertEqual(command.target_balance, Decimal("150000"))
        self.assertEqual(command.status, CommandStatus.APPLIED)

    def test_legacy_price_field_is_visible_compatibility(self):
        command = parse_set_balance_command({"action": "對帳", "source_row_id": "Form:legacy-2", "asset_type": "現金_TWD", "symbol": "TWD", "currency": "TWD", "price": "150000", "description": "現金餘額"})
        self.assertEqual(command.status, CommandStatus.APPLIED_WITH_COMPATIBILITY)
        self.assertEqual(command.compatibility_used, "legacy_target_from_price_field")

    def test_legacy_form_replace_cash_row_becomes_set_balance(self):
        headers = ["Timestamp", "Email Address", "approved", "currency", "unit", "asset_type", "symbol", "action", "quantity", "price", "交易內容"]
        row = ["2026-08-14T14:45:00+08:00", "owner@example.com", "true", "TWD", "TWD", "現金_TWD", "TWD", "取代", "0", "150000", ""]
        result = parse_transaction_rows(headers, [row], source_sheet="Form")
        self.assertEqual(result.accepted[0].action, Action.SET_BALANCE)
        self.assertEqual(result.accepted[0].quantity, Decimal("150000"))
        self.assertEqual(command_from_transaction(result.accepted[0]).status, CommandStatus.APPLIED_WITH_COMPATIBILITY)

    def test_original_cash_snapshot_description_uses_legacy_price_target(self):
        headers = ["Timestamp", "Email Address", "approved", "currency", "unit", "asset_type", "symbol", "action", "quantity", "price", "交易內容"]
        row = ["2026-08-14T14:45:00+08:00", "owner@example.com", "true", "TWD", "TWD", "現金_TWD", "TWD", "", "", "150000", "取代台幣現金金額"]
        result = parse_transaction_rows(headers, [row], source_sheet="Form")
        self.assertEqual(result.accepted[0].action, Action.SET_BALANCE)
        self.assertEqual(result.accepted[0].quantity, Decimal("150000"))
        self.assertEqual(result.accepted[0].compatibility_used, "legacy_target_from_price_field")

    def test_ingestion_contract_has_summary_and_actionable_rejection(self):
        rejected = parse_transaction_rows(
            ["Timestamp", "Email Address", "交易內容"],
            [["2026-08-14T14:45:00+08:00", "owner@example.com", "SET_BALANCE TWD"]],
            source_sheet="Form",
        ).rejected
        rows = build_ingestion_status(rejected=rejected)
        contract = build_ingestion_contract(rows)
        self.assertEqual(contract["summary"]["rejected"], 1)
        self.assertTrue(contract["recent"][0]["reason"])

    def test_ambiguous_and_unsafe_values_fail_closed(self):
        with self.assertRaises(CommandValidationError):
            parse_set_balance_command({"command": "SET_BALANCE", "source_row_id": "Form:2", "asset_type": "現金_TWD", "symbol": "TWD", "currency": "TWD"})
        with self.assertRaises(CommandValidationError):
            parse_set_balance_command({"command": "SET_BALANCE", "source_row_id": "Form:2", "asset_type": "現金_TWD", "symbol": "TWD", "currency": "TWD", "target_balance": "-1"})

    def test_reconciliation_sets_exact_balance_and_is_idempotent(self):
        ledger = PortfolioLedger([tx(Action.DEPOSIT, quantity="187000", transaction_id="00000000-0000-0000-0000-000000000010")])
        event = ledger.reconcile_cash_balance("TWD", Decimal("150000"), transaction_id="00000000-0000-0000-0000-000000000011")
        self.assertEqual(ledger.state.cash_balance("TWD"), Decimal("150000"))
        self.assertFalse(ledger.append(event))
        self.assertEqual(ledger.state.cash_balance("TWD"), Decimal("150000"))

    def test_reconciliation_increase_and_duplicate_conflict(self):
        ledger = PortfolioLedger([tx(Action.DEPOSIT, quantity="120000", transaction_id="00000000-0000-0000-0000-000000000020")])
        event = ledger.reconcile_cash_balance("TWD", Decimal("150000"), transaction_id="00000000-0000-0000-0000-000000000021")
        self.assertEqual(ledger.state.cash_balance("TWD"), Decimal("150000"))
        changed = Transaction(**{**event.__dict__, "quantity": Decimal("151000")})
        with self.assertRaises(PortfolioLedgerConflict):
            ledger.append(changed)

    def test_legacy_inventory_and_canonical_event_share_final_cash(self):
        inventory = {"現金_TWD": {"TWD": 187000.0}, "現金_USD": {"USD": 0.0}}
        command = parse_set_balance_command({"command": "SET_BALANCE", "source_row_id": "Form:coherence", "asset_type": "現金_TWD", "symbol": "TWD", "currency": "TWD", "target_balance": "150000"})
        updated, events = apply_reconciliation_events(inventory, [command.transaction])
        self.assertEqual(inventory["現金_TWD"]["TWD"], 150000.0)
        self.assertEqual(updated[0].reconciliation_delta, Decimal("150000") - Decimal("187000"))
        self.assertEqual(events[0]["eventType"], "RECONCILIATION_DECREASE")

    def test_non_cash_legacy_snapshot_does_not_enter_cash_reconciliation(self):
        inventory = {
            "現金_TWD": {"TWD": 187000.0},
            "現金_USD": {"USD": 0.0},
            "質押負債": {"Current_Debt": 0.0, "History": []},
        }
        debt = tx(
            Action.SET_BALANCE,
            quantity="1870000",
            symbol="TWD",
            asset_type="質押負債",
            transaction_id="00000000-0000-0000-0000-000000000041",
        )
        updated, events = apply_reconciliation_events(inventory, [debt])
        self.assertEqual(updated, (debt,))
        self.assertEqual(events, [])
        self.assertEqual(inventory["現金_TWD"]["TWD"], 187000.0)

    def test_reconciliation_is_not_external_flow_or_market_pnl(self):
        ledger = PortfolioLedger([tx(Action.DEPOSIT, quantity="120000", transaction_id="00000000-0000-0000-0000-000000000031")])
        reconciliation = ledger.reconcile_cash_balance("TWD", Decimal("150000"), transaction_id="00000000-0000-0000-0000-000000000030")
        performance = performance_breakdown(150000, 120000, [reconciliation])
        self.assertEqual(performance["externalCashFlow"], 0)
        self.assertEqual(performance["reconciliationAdjustment"], 30000)
        self.assertEqual(performance["marketPnl"], 0)
        attribution = build_pnl_attribution(120000, 150000, {}, {}, reconciliation_adjustment=30000)
        self.assertEqual(attribution["reconciliationAdjustment"], 30000)
        self.assertTrue(attribution["reconciled"])


if __name__ == "__main__":
    unittest.main()
