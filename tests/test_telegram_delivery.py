import unittest
from types import SimpleNamespace

from telegram_delivery import (
    TelegramDeliveryError, decrypt_outbox, deliver_once, encrypt_outbox, send_message,
)


class TelegramDeliveryTests(unittest.TestCase):
    def test_success_requires_api_receipt(self):
        result = send_message(
            "test-token", {"chat_id": "test", "text": "fixture"},
            post=lambda *args, **kwargs: SimpleNamespace(
                status_code=200, json=lambda: {"ok": True, "result": {"message_id": 42}}
            ),
        )
        self.assertEqual(result, {"status": "SENT", "messageId": "42"})

    def test_malformed_or_rejected_responses_never_report_sent(self):
        for response in (
            SimpleNamespace(status_code=200, json=lambda: {"ok": False, "description": "rejected"}),
            SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {}}),
            SimpleNamespace(status_code=500, json=lambda: {"ok": False}),
        ):
            with self.subTest(response=response.status_code):
                with self.assertRaises(TelegramDeliveryError):
                    send_message("test-token", {"chat_id": "test", "text": "fixture"}, post=lambda *a, **k: response)

    def test_429_uses_retry_after_and_retries_at_most_three_times(self):
        responses = [
            SimpleNamespace(status_code=429, json=lambda: {"ok": False, "parameters": {"retry_after": 2}}),
            SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {"message_id": 43}}),
        ]
        calls, waits = [], []
        result = send_message(
            "test-token", {"chat_id": "test", "text": "fixture"},
            post=lambda *a, **k: calls.append(1) or responses.pop(0), sleep=waits.append,
        )
        self.assertEqual(result["messageId"], "43")
        self.assertEqual(len(calls), 2)
        self.assertEqual(waits, [2.0])

    def test_write_ahead_and_ambiguous_delivery_prevent_resend(self):
        state = {"status": "PENDING"}
        sends = []
        response = SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {"message_id": 44}})
        result = deliver_once(
            date_key="2026-10-08", window="tw", payload={"text": "fixture"}, token="test-token",
            load_state=lambda: dict(state), save_state=lambda value: (state.update(value), None)[1],
            post=lambda *a, **k: sends.append(1) or response, now="2026-10-08T06:00:00Z",
        )
        self.assertEqual(result["status"], "SENT")
        self.assertEqual(state["status"], "SENT")
        second = deliver_once(
            date_key="2026-10-08", window="tw", payload={"text": "fixture"}, token="test-token",
            load_state=lambda: {"status": "SENDING"}, save_state=lambda value: None,
            post=lambda *a, **k: sends.append(1), now="2026-10-08T06:01:00Z",
        )
        self.assertEqual(second["status"], "DELIVERY_UNKNOWN")
        self.assertEqual(len(sends), 1)

    def test_public_artifact_outbox_is_authenticated_and_encrypted(self):
        envelope = {
            "schemaVersion": 1, "notificationType": "settlement", "windowDate": "2026-10-08",
            "window": "tw", "sourceCommit": "a" * 40, "runId": "123",
            "payload": {"chat_id": "private-chat", "text": "private portfolio value"},
        }
        encrypted = encrypt_outbox(envelope, telegram_token="fixture-token", google_credentials="fixture-gcp")
        self.assertNotIn("private portfolio value", str(encrypted))
        self.assertNotIn("private-chat", str(encrypted))
        self.assertEqual(
            decrypt_outbox(encrypted, telegram_token="fixture-token", google_credentials="fixture-gcp"),
            envelope,
        )
        encrypted["ciphertext"] = encrypted["ciphertext"][:-4] + "AAAA"
        with self.assertRaisesRegex(ValueError, "authentication failed"):
            decrypt_outbox(encrypted, telegram_token="fixture-token", google_credentials="fixture-gcp")


if __name__ == "__main__":
    unittest.main()
