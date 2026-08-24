import unittest

from history_store import (
    build_header_map,
    column_to_a1,
    ensure_history_columns,
    ledger_conflict_alert_sent,
    mark_ledger_conflict_alert_sent,
    mark_schema_drift_alert_sent,
    schema_drift_alert_sent,
    upsert_history_snapshot,
)


class FakeHistorySheet:
    def __init__(self, rows):
        self.rows = [list(row) for row in rows]
        self.updates = []

    def row_values(self, row_number):
        return list(self.rows[row_number - 1]) if row_number <= len(self.rows) else []

    def get_all_values(self):
        return [list(row) for row in self.rows]

    def update_cell(self, row_number, column_number, value):
        while len(self.rows) < row_number:
            self.rows.append([])
        row = self.rows[row_number - 1]
        while len(row) < column_number:
            row.append("")
        row[column_number - 1] = value
        self.updates.append((f"cell:{row_number},{column_number}", value))

    def update(self, range_name, values):
        self.updates.append((range_name, values))
        # Tests use single-cell A1 writes; emulate that operation.
        column = 0
        for char in range_name:
            if char.isalpha():
                column = column * 26 + ord(char.upper()) - 64
            else:
                break
        row_number = int("".join(char for char in range_name if char.isdigit()))
        while len(self.rows) < row_number:
            self.rows.append([])
        row = self.rows[row_number - 1]
        while len(row) < column:
            row.append("")
        row[column - 1] = values[0][0]

    def append_row(self, row):
        self.rows.append(list(row))


class HistoryStoreTests(unittest.TestCase):
    def test_a1_conversion_supports_columns_beyond_z(self):
        self.assertEqual(column_to_a1(1), "A")
        self.assertEqual(column_to_a1(26), "Z")
        self.assertEqual(column_to_a1(27), "AA")
        self.assertEqual(column_to_a1(52), "AZ")
        self.assertEqual(column_to_a1(53), "BA")

    def test_duplicate_headers_are_rejected(self):
        with self.assertRaises(ValueError):
            build_header_map(["Date", "Net_Asset", "Date"])

    def test_reordered_headers_update_only_named_fields(self):
        sheet = FakeHistorySheet([
            ["Date", "Settlement_Notification_Sent_At", "Net_Asset", "Total_Asset"],
            ["2026-08-04", '{"tw":"already-sent"}', "90", "100"],
        ])
        result = upsert_history_snapshot(sheet, {
            "Date": "2026-08-04",
            "Total_Asset": 120,
            "Net_Asset": 110,
        })
        self.assertEqual(result, "updated")
        self.assertEqual(sheet.rows[1], ["2026-08-04", '{"tw":"already-sent"}', 110, 120])
        self.assertTrue(any(update[0] == "C2" for update in sheet.updates))
        self.assertTrue(any(update[0] == "D2" for update in sheet.updates))
        self.assertFalse(any(update[0] == "B2" for update in sheet.updates))

    def test_new_columns_and_rows_preserve_schema(self):
        sheet = FakeHistorySheet([["Date", "Telegram_Marker"]])
        ensure_history_columns(sheet, ["Total_Asset"])
        result = upsert_history_snapshot(sheet, {
            "Date": "2026-08-05",
            "Total_Asset": 123.45,
        })
        self.assertEqual(result, "created")
        self.assertEqual(sheet.rows[0], ["Date", "Telegram_Marker", "Total_Asset"])
        self.assertEqual(sheet.rows[1], ["2026-08-05", "", 123.45])

    def test_conflict_alert_digest_is_deduplicated_across_dates(self):
        sheet = FakeHistorySheet([
            ["Date", "Ledger_Conflict_Alert_Marker"],
            ["2026-08-21", "{}"],
            ["2026-08-22", "{}"],
        ])
        mark_ledger_conflict_alert_sent(sheet, "2026-08-21", "digest-a", "2026-08-21T05:40:00+08:00")
        self.assertTrue(ledger_conflict_alert_sent(sheet, "2026-08-22", "digest-a"))
        self.assertFalse(ledger_conflict_alert_sent(sheet, "2026-08-22", "digest-b"))

    def test_schema_drift_digest_is_deduplicated_across_dates(self):
        sheet = FakeHistorySheet([
            ["Date", "Schema_Drift_Alert_Marker"],
            ["2026-08-23", "{}"],
            ["2026-08-24", "{}"],
        ])
        mark_schema_drift_alert_sent(sheet, "2026-08-23", "shape-a", "2026-08-23T05:40:00+08:00")
        self.assertTrue(schema_drift_alert_sent(sheet, "shape-a"))
        self.assertFalse(schema_drift_alert_sent(sheet, "shape-b"))
        mark_schema_drift_alert_sent(sheet, "2026-08-24", "shape-b", "2026-08-24T05:40:00+08:00")
        self.assertTrue(schema_drift_alert_sent(sheet, "shape-b"))

    def test_alert_digest_can_be_marked_without_todays_snapshot(self):
        sheet = FakeHistorySheet([
            ["Date", "Schema_Drift_Alert_Marker"],
            ["2026-08-23", "{}"],
        ])
        mark_schema_drift_alert_sent(sheet, "2026-08-24", "blocked-a", "2026-08-24T05:40:00+08:00")
        self.assertTrue(schema_drift_alert_sent(sheet, "blocked-a"))


if __name__ == "__main__":
    unittest.main()
