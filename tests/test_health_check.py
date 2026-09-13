import datetime
import unittest
from unittest.mock import patch

from health_check import (
    DEFAULT_STALE_AFTER_HOURS,
    GROWTH_STALE_AFTER_HOURS,
    GROWTH_URL,
    TAIPEI,
    evaluate_status,
    parse_generated_at,
    send_alert,
)


class HealthCheckTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(2026, 7, 29, 18, 0, tzinfo=TAIPEI)

    def test_accepts_fresh_healthy_contract(self):
        payload = {
            "status": "ok", "generatedAt": "2026-07-29T16:00:00+08:00",
            "freshness": {"staleAfterHours": 18}, "sources": {"googleSheet": "ok"},
        }
        self.assertEqual(evaluate_status("Growth", payload, self.now), [])

    def test_reports_degraded_stale_and_source_failure(self):
        payload = {
            "status": "degraded", "generatedAt": "2026-07-28T16:00:00+08:00",
            "staleAfterHours": 18, "sources": {"vix": "unavailable"},
        }
        issues = evaluate_status("Skynet", payload, self.now)
        self.assertEqual(len(issues), 3)
        self.assertIn("status=degraded", issues[0])
        self.assertIn("stale", issues[1])
        self.assertIn("source vix", issues[2])

    def test_interprets_legacy_growth_timestamp_as_taipei(self):
        parsed = parse_generated_at("2026-07-29T16:00:00")
        self.assertEqual(parsed.tzinfo, TAIPEI)
        self.assertEqual(parsed.hour, 16)

    def test_growth_missing_threshold_uses_72_hour_default(self):
        for age_hours in (18, 48.1, 71.99, 72):
            generated = self.now - datetime.timedelta(hours=age_hours)
            payload = {
                "status": "ok",
                "generatedAt": generated.isoformat(),
                "sources": {"googleSheet": "ok"},
            }
            self.assertEqual(evaluate_status("Growth Dashboard", payload, self.now), [])
        generated = self.now - datetime.timedelta(hours=72.01)
        issues = evaluate_status(
            "Growth Dashboard",
            {"status": "ok", "generatedAt": generated.isoformat()},
            self.now,
        )
        self.assertEqual(len(issues), 1)
        self.assertIn("limit 72h", issues[0])

    def test_skynet_missing_threshold_keeps_18_hour_default(self):
        generated = self.now - datetime.timedelta(hours=18.01)
        issues = evaluate_status(
            "Skynet Monitoring",
            {"status": "ok", "generatedAt": generated.isoformat()},
            self.now,
        )
        self.assertEqual(len(issues), 1)
        self.assertIn("limit 18h", issues[0])
        self.assertEqual(DEFAULT_STALE_AFTER_HOURS, 18)
        self.assertEqual(GROWTH_STALE_AFTER_HOURS, 72)

    def test_health_alert_uses_shared_growth_button_label(self):
        with patch.dict(
            "os.environ",
            {"TELEGRAM_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"},
            clear=False,
        ), patch("requests.post") as post:
            post.return_value.raise_for_status.return_value = None
            send_alert(["Growth Dashboard stale for 73.0h (limit 72h)"])
        payload = post.call_args.kwargs["json"]
        button = payload["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "🌱SFC.e Growth")
        self.assertEqual(button["web_app"]["url"], GROWTH_URL)


if __name__ == "__main__":
    unittest.main()
