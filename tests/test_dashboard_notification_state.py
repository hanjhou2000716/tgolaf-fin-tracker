import json
import unittest
from types import SimpleNamespace

from dashboard_pipeline import mark_settlement_notification_state, settlement_notification_state
from settlement_notification_state import NotificationStateError, resolve_history_sheet


class FakeHistorySheet:
    def __init__(self, marker=None, rows=None):
        self.rows = rows or [
            ["Date", "Settlement_Notification_Sent_At"],
            ["2026-10-08", marker or ""],
        ]

    def row_values(self, row_number):
        return list(self.rows[row_number - 1])

    def get_all_values(self):
        return [list(row) for row in self.rows]

    def update(self, cell, values):
        import re
        row_number = int(re.fullmatch(r"[A-Z]+(\d+)", cell).group(1))
        while len(self.rows[row_number - 1]) < 2:
            self.rows[row_number - 1].append("")
        self.rows[row_number - 1][1] = values[0][0]


class DashboardNotificationStateTests(unittest.TestCase):
    def test_sender_and_recovery_share_one_unambiguous_history_resolver(self):
        history = SimpleNamespace(title="History")
        archive = SimpleNamespace(title="Archive")
        role_for = lambda title: "HISTORY" if title == "History" else "LEGACY_ARCHIVE"
        self.assertIs(resolve_history_sheet([archive, history], role_for), history)
        with self.assertRaisesRegex(NotificationStateError, "NOTIFICATION_HISTORY_IDENTITY_AMBIGUOUS"):
            resolve_history_sheet([history, SimpleNamespace(title="History copy")], lambda _title: "HISTORY")

    def test_legacy_plain_iso_timestamp_is_safely_treated_as_sent(self):
        result = settlement_notification_state(
            FakeHistorySheet("2026-10-08T05:40:00Z"), "2026-10-08", "us"
        )
        self.assertEqual(result["status"], "SENT")
        self.assertTrue(result["legacy"])

    def test_legacy_json_encoded_iso_timestamp_is_safely_treated_as_sent(self):
        result = settlement_notification_state(
            FakeHistorySheet(json.dumps("2026-10-08T05:40:00Z")), "2026-10-08", "us"
        )
        self.assertEqual(result["status"], "SENT")
        self.assertTrue(result["legacy"])

    def test_malformed_legacy_marker_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "malformed"):
            settlement_notification_state(FakeHistorySheet("not-a-timestamp"), "2026-10-08", "us")

    def test_old_plain_timestamps_do_not_break_missing_afternoon_window(self):
        sheet = FakeHistorySheet(rows=[
            ["Date", "Settlement_Notification_Sent_At"],
            ["2026-07-29", "2026-07-29 16:26:12"],
            ["2026-07-30", "2026-07-30 14:26:17"],
            ["2026-07-31", "2026-07-31 14:10:34"],
            ["2026-10-16", '{"us":{"status":"SENT","sentAt":"2026-10-16T05:40:00Z"}}'],
        ])
        result = settlement_notification_state(sheet, "2026-10-16", "tw")
        self.assertEqual(result["status"], "PENDING")

    def test_date_qualified_receipt_on_older_row_is_found_before_pending(self):
        sheet = FakeHistorySheet(rows=[
            ["Date", "Settlement_Notification_Sent_At"],
            ["2026-10-15", '{"2026-10-16|tw":{"status":"SENT","sentAt":"2026-10-16T06:45:00Z","messageId":"88"}}'],
            ["2026-10-16", ""],
        ])
        result = settlement_notification_state(sheet, "2026-10-16", "tw")
        self.assertEqual(result["status"], "SENT")
        self.assertEqual(result["messageId"], "88")

    def test_writer_uses_date_qualified_key_and_preserves_other_window(self):
        sheet = FakeHistorySheet(rows=[
            ["Date", "Settlement_Notification_Sent_At"],
            ["2026-10-16", '{"us":{"status":"SENT","sentAt":"2026-10-16T05:40:00Z"}}'],
        ])
        sending = {"status": "SENDING", "attemptId": "attempt-1", "attemptedAt": "2026-10-16T06:45:00Z"}
        mark_settlement_notification_state(sheet, "2026-10-16", "tw", sending)
        result = settlement_notification_state(sheet, "2026-10-16", "tw")
        marker = json.loads(sheet.rows[1][1])
        self.assertEqual(result["status"], "SENDING")
        self.assertEqual(result["attemptId"], "attempt-1")
        self.assertIn("us", marker)
        self.assertIn("2026-10-16|tw", marker)

    def test_conflicting_duplicate_receipts_fail_closed(self):
        sheet = FakeHistorySheet(rows=[
            ["Date", "Settlement_Notification_Sent_At"],
            ["2026-10-16", '{"2026-10-16|tw":{"status":"SENT","messageId":"1"}}'],
            ["2026-10-16", '{"tw":{"status":"SENDING","attemptId":"other"}}'],
        ])
        with self.assertRaisesRegex(RuntimeError, "conflicting states"):
            settlement_notification_state(sheet, "2026-10-16", "tw")

    def test_versioned_marker_hash_is_verified_before_reuse(self):
        sheet = FakeHistorySheet()
        mark_settlement_notification_state(sheet, "2026-10-08", "tw", {
            "status": "SENDING", "attemptId": "attempt-1", "attemptedAt": "2026-10-08T06:45:00Z",
        })
        marker = json.loads(sheet.rows[1][1])
        marker["2026-10-08|tw"]["status"] = "SENT"
        sheet.rows[1][1] = json.dumps(marker)
        with self.assertRaisesRegex(RuntimeError, "marker hash mismatch"):
            settlement_notification_state(sheet, "2026-10-08", "tw")


if __name__ == "__main__":
    unittest.main()
