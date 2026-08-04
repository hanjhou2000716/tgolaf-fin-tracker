import unittest
from datetime import date
from decimal import Decimal

from transaction_schema import TransactionSchemaError, parse_quantity, parse_transaction_rows


HEADERS = [
    "transaction_id",
    "Timestamp",
    "Email Address",
    "approved",
    "transaction_date",
    "asset_type",
    "symbol",
    "action",
    "quantity",
    "unit",
    "currency",
    "price",
]


def row(transaction_id, approved="true", action="BUY", quantity="1", unit="張"):
    return [transaction_id, "2026-08-04T12:00:00+08:00", "owner@example.com", approved, "2026-08-04", "TW", "006208", action, quantity, unit, "TWD", "100"]


class TransactionSchemaTests(unittest.TestCase):
    def test_header_order_is_explicit_and_quantity_lot_is_normalized(self):
        result = parse_transaction_rows(HEADERS, [row("00000000-0000-0000-0000-000000000001")], source_sheet="Form")
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(result.accepted[0].quantity, Decimal("1000"))
        self.assertEqual(result.accepted[0].unit, "SHARE")
        self.assertEqual(result.accepted[0].transaction_date, date(2026, 8, 4))

    def test_unapproved_transaction_goes_to_pending_queue(self):
        result = parse_transaction_rows(HEADERS, [row("00000000-0000-0000-0000-000000000002", approved="false")], source_sheet="Form")
        self.assertEqual(len(result.accepted), 0)
        self.assertEqual([item.transaction_id for item in result.pending], ["00000000-0000-0000-0000-000000000002"])

    def test_duplicate_id_is_rejected_and_not_counted_twice(self):
        txid = "00000000-0000-0000-0000-000000000003"
        result = parse_transaction_rows(HEADERS, [row(txid), row(txid)], source_sheet="Form")
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(result.rejected[0].detail, "duplicate_transaction_id")

    def test_unknown_action_and_unit_are_rejected(self):
        result = parse_transaction_rows(HEADERS, [row("00000000-0000-0000-0000-000000000004", action="REPLACE", unit="mystery")], source_sheet="Form")
        self.assertEqual(len(result.accepted), 0)
        self.assertEqual(len(result.rejected), 1)

    def test_missing_required_header_fails_closed(self):
        with self.assertRaises(TransactionSchemaError):
            parse_transaction_rows(HEADERS[:-2], [], source_sheet="Form")

    def test_explicit_quantity_units_only(self):
        self.assertEqual(parse_quantity("1", "張"), (Decimal("1000"), "SHARE"))
        self.assertEqual(parse_quantity("25", "%"), (Decimal("0.25"), "PERCENT"))
        with self.assertRaises(ValueError):
            parse_quantity("1", "模糊單位")


if __name__ == "__main__":
    unittest.main()
