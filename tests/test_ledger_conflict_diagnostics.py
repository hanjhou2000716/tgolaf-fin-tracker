import unittest

from ledger_conflict_diagnostics import ledger_conflict_digest, ledger_conflict_summary_artifact


def _payload(*, price, marker="settlement_quote_estimate:2026-08-29", quantity="100"):
    return {
        "transaction_id": "11111111-1111-4111-8111-111111111111",
        "source_row_id": "表單回覆 3:28",
        "submitted_at": "2026-08-29T20:55:24+08:00",
        "transaction_date": "2026-08-29",
        "asset_type": "TW_STOCK",
        "symbol": "006208",
        "action": "BUY",
        "quantity": quantity,
        "unit": "SHARE",
        "currency": "TWD",
        "price": price,
        "compatibility_used": marker,
    }


class LedgerConflictDiagnosticsTests(unittest.TestCase):
    def test_derived_price_churn_keeps_same_digest(self):
        before = _payload(price="100")
        after = _payload(price="105")
        conflict = {
            "transaction_id": before["transaction_id"],
            "matched_existing_transaction_id": before["transaction_id"],
            "source_row_id": before["source_row_id"],
            "changed_fields": ["price"],
            "existing_payload": before,
            "current_payload": after,
        }
        later = {**conflict, "current_payload": _payload(price="106")}
        self.assertEqual(ledger_conflict_digest([conflict]), ledger_conflict_digest([later]))
        summary = ledger_conflict_summary_artifact(
            [conflict],
            {"coreConflictCount": 1, "changedFieldCounts": {"price": 1}, "derivedPriceReplayCount": 0},
        )
        self.assertEqual(summary["priceOnlyConflictCount"], 0)
        self.assertEqual(summary["conflicts"][0]["changedFields"], ["price"])
        self.assertNotIn("100", str(summary))
        self.assertNotIn("105", str(summary))

    def test_explicit_core_change_changes_digest(self):
        before = _payload(price="100", marker=None)
        after = _payload(price="105", marker=None)
        conflict = {
            "transaction_id": before["transaction_id"],
            "matched_existing_transaction_id": before["transaction_id"],
            "source_row_id": before["source_row_id"],
            "changed_fields": ["price"],
            "existing_payload": before,
            "current_payload": after,
        }
        changed = {**conflict, "current_payload": {**after, "quantity": "101"}, "changed_fields": ["price", "quantity"]}
        self.assertNotEqual(ledger_conflict_digest([conflict]), ledger_conflict_digest([changed]))

    def test_legacy_serialization_variants_keep_same_digest(self):
        before = _payload(price="100", marker="legacy_mixed_form_row")
        after = {
            **before,
            "action": "買入",
            "unit": " share ",
            "currency": "twd",
            "transaction_date": "2026/08/29",
            "compatibility_used": " LEGACY_MIXED_FORM_ROW ",
        }
        conflict = {
            "transaction_id": before["transaction_id"],
            "matched_existing_transaction_id": before["transaction_id"],
            "source_row_id": before["source_row_id"],
            "changed_fields": ["action", "transaction_date", "unit", "currency"],
            "existing_payload": before,
            "current_payload": after,
        }
        self.assertEqual(ledger_conflict_digest([conflict]), ledger_conflict_digest([{**conflict, "current_payload": before}]))


if __name__ == "__main__":
    unittest.main()
