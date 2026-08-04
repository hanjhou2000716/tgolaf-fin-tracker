from pathlib import Path
import unittest


class LedgerSyncContractTests(unittest.TestCase):
    def test_loader_result_is_consumed_by_main(self):
        source = Path("dashboard_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("accepted_transactions, ledger_sync_result = calculate_current_assets()", source)
        self.assertIn("return inventory, history_sheet, accepted_transactions, ledger_sync_result", source)


if __name__ == "__main__":
    unittest.main()
