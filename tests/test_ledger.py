import unittest
from datetime import date
from decimal import Decimal

from ledger import ImmutableLedger, LedgerConflictError
from transaction_schema import Action, Transaction


def transaction(transaction_id, *, action=Action.BUY, reversal_of=None, quantity="1"):
    return Transaction(
        transaction_id=transaction_id,
        source_row_id="Form:2",
        submitted_at="2026-08-04T00:00:00Z",
        submitter_email="owner@example.com",
        approved=True,
        transaction_date=date(2026, 8, 4),
        asset_type="TW_STOCK",
        symbol="006208",
        action=action,
        quantity=Decimal(quantity),
        unit="SHARE",
        currency="TWD",
        price=Decimal("100"),
        reversal_of=reversal_of,
    )


class LedgerTests(unittest.TestCase):
    def test_replay_is_idempotent(self):
        entry = transaction("11111111-1111-4111-8111-111111111111")
        ledger = ImmutableLedger()
        self.assertEqual(ledger.apply([entry, entry]), 1)
        self.assertEqual(len(ledger.entries), 1)

    def test_same_id_with_different_content_is_rejected(self):
        transaction_id = "22222222-2222-4222-8222-222222222222"
        ledger = ImmutableLedger([{
            **entry_payload(transaction(transaction_id, quantity="1")),
        }])
        with self.assertRaises(LedgerConflictError):
            ledger.append(transaction(transaction_id, quantity="2"))

    def test_correction_requires_reversal_of_existing_entry(self):
        original_id = "33333333-3333-4333-8333-333333333333"
        reversal_id = "44444444-4444-4444-8444-444444444444"
        ledger = ImmutableLedger()
        with self.assertRaises(ValueError):
            ledger.append(transaction(reversal_id, action=Action.REVERSAL))
        ledger.append(transaction(original_id))
        self.assertTrue(ledger.append(transaction(
            reversal_id, action=Action.REVERSAL, reversal_of=original_id
        )))


def entry_payload(item):
    from ledger import transaction_payload
    return transaction_payload(item)


if __name__ == "__main__":
    unittest.main()
