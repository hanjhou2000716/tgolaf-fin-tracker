import datetime as dt
import hashlib
import json
import unittest

from recover_settlement_notification import recover
from telegram_delivery import encrypt_outbox


class FakeHistory:
    def __init__(self):
        self.rows = [
            ["Date", "Settlement_Notification_Sent_At"],
            ["2026-10-08", "{}"],
        ]

    def row_values(self, row):
        return list(self.rows[row - 1])

    def get_all_values(self):
        return [list(row) for row in self.rows]

    def update(self, cell, values):
        import re
        match = re.fullmatch(r"([A-Z]+)(\d+)", cell)
        column, row = 2, int(match.group(2))
        while len(self.rows[row - 1]) < column:
            self.rows[row - 1].append("")
        self.rows[row - 1][column - 1] = values[0][0]


def outbox(run_id="100", commit="a" * 40):
    value = {
        "schemaVersion": 1,
        "notificationType": "settlement",
        "windowDate": "2026-10-08",
        "window": "tw",
        "sourceCommit": commit,
        "runId": run_id,
        "payload": {"chat_id": "test-chat", "text": "fixture"},
    }
    value["contentHash"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return value


class NotificationRecoveryTests(unittest.TestCase):
    def test_valid_same_day_outbox_sends_once_and_writes_history_receipt(self):
        sheet = FakeHistory()
        calls = []
        response = type("Response", (), {
            "status_code": 200,
            "json": lambda self: {"ok": True, "result": {"message_id": 88}},
        })()
        result = recover(
            outbox(), token="test-token", chat_id="test-chat", history_sheet=sheet,
            expected_run_id="100", expected_commit="a" * 40,
            post=lambda *args, **kwargs: calls.append(1) or response,
            now=dt.datetime(2026, 10, 8, 7, 0, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(result["status"], "SENT")
        self.assertEqual(len(calls), 1)
        self.assertIn('"status":"SENT"', sheet.rows[1][1])

    def test_failed_or_mismatched_artifacts_do_not_trigger_a_send(self):
        encrypted = encrypt_outbox(outbox(), telegram_token="fixture-token", google_credentials="fixture-gcp")
        import tempfile
        from pathlib import Path
        from telegram_delivery import decrypt_outbox
        decoded = decrypt_outbox(encrypted, telegram_token="fixture-token", google_credentials="fixture-gcp")
        self.assertEqual(decoded["runId"], "100")
        bad = dict(encrypted)
        bad["runId"] = "999"
        with self.assertRaisesRegex(ValueError, "authentication failed"):
            decrypt_outbox(bad, telegram_token="fixture-token", google_credentials="fixture-gcp")

    def test_invalid_hash_wrong_run_and_old_date_are_never_sent(self):
        value = outbox()
        value["payload"]["text"] = "tampered"
        result = recover(value, token="t", chat_id="test-chat", history_sheet=FakeHistory(),
                         expected_run_id="100", expected_commit="a" * 40,
                         post=lambda *a, **k: self.fail("must not send"),
                         now=dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc))
        self.assertEqual(result["reasonCode"], "OUTBOX_HASH_INVALID")
        result = recover(outbox(), token="t", chat_id="test-chat", history_sheet=FakeHistory(),
                         expected_run_id="101", expected_commit="a" * 40,
                         post=lambda *a, **k: self.fail("must not send"),
                         now=dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc))
        self.assertEqual(result["reasonCode"], "OUTBOX_RUN_IDENTITY_MISMATCH")
        value = outbox()
        value.pop("contentHash")
        value["windowDate"] = "2026-10-07"
        value["contentHash"] = hashlib.sha256(
            json.dumps({k: v for k, v in value.items() if k != "contentHash"}, sort_keys=True,
                       ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        result = recover(value, token="t", chat_id="test-chat", history_sheet=FakeHistory(),
                         expected_run_id="100", expected_commit="a" * 40,
                         post=lambda *a, **k: self.fail("must not send"),
                         now=dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc))
        self.assertEqual(result["status"], "EXPIRED")


if __name__ == "__main__":
    unittest.main()
