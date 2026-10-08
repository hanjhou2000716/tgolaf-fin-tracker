import json
import unittest

from dashboard_pipeline import settlement_notification_state


class FakeHistorySheet:
    def __init__(self, marker):
        self.rows = [
            ["Date", "Settlement_Notification_Sent_At"],
            ["2026-10-08", marker],
        ]

    def row_values(self, row_number):
        return list(self.rows[row_number - 1])

    def get_all_values(self):
        return [list(row) for row in self.rows]


class DashboardNotificationStateTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
