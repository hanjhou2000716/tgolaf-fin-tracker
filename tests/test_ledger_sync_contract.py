from pathlib import Path
import unittest
from datetime import date
from decimal import Decimal

from transaction_command import inventory_rows_from_transactions
from transaction_schema import Action, Transaction


class LedgerSyncContractTests(unittest.TestCase):
    def test_set_balance_is_not_replayed_by_legacy_inventory_adapter(self):
        reconciliation = Transaction(
            transaction_id="00000000-0000-0000-0000-000000000099",
            source_row_id="Form:99",
            submitted_at="2026-08-14T14:45:00+08:00",
            submitter_email="owner@example.com",
            approved=True,
            transaction_date=date(2026, 8, 14),
            asset_type="現金_TWD",
            symbol="TWD",
            action=Action.SET_BALANCE,
            quantity=Decimal("150000"),
            unit="TWD",
            currency="TWD",
        )
        buy = Transaction(
            **{
                **reconciliation.__dict__,
                "transaction_id": "00000000-0000-0000-0000-000000000100",
                "asset_type": "台股",
                "symbol": "006208",
                "action": Action.BUY,
                "quantity": Decimal("1"),
                "price": Decimal("100"),
                "unit": "股",
            }
        )
        rows = (
            ("2026-08-14", "現金_TWD", "TWD", "取代", "150000"),
            ("2026-08-14", "台股", "006208", "買入", "1"),
        )
        self.assertEqual(inventory_rows_from_transactions((reconciliation, buy), rows), (rows[1],))

    def test_loader_result_is_consumed_by_main(self):
        source = Path("dashboard_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("accepted_transactions, ledger_sync_result = calculate_current_assets()", source)
        self.assertIn("return inventory, history_sheet, accepted_transactions, ledger_sync_result", source)


if __name__ == "__main__":
    unittest.main()
