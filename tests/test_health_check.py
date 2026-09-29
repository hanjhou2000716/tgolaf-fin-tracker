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

    def test_future_timestamp_is_never_healthy(self):
        payload = {
            "status": "ok",
            "generatedAt": (self.now + datetime.timedelta(minutes=5, seconds=1)).isoformat(),
            "freshness": {"staleAfterHours": 72},
        }
        issues = evaluate_status("Growth Dashboard", payload, self.now)
        self.assertEqual(len(issues), 1)
        self.assertIn("generatedAt is in the future", issues[0])

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
        self.assertEqual(button["text"], "🌱 SFC.e Growth")
        self.assertEqual(button["web_app"]["url"], GROWTH_URL)

    def _skynet_v2(self, *, generated="2026-09-29T07:56:00+08:00", window="morning",
                   window_date="2026-09-29", tw_latest="2026-09-24", tw_expected="2026-09-24",
                   tw_status="market_closed", tw_due="2026-09-29T14:30:00+08:00"):
        return {
            "schemaVersion": 2,
            "status": "ok",
            "generatedAt": generated,
            "service": {"status": "ok", "generatedAt": generated,
                        "windowDate": window_date, "window": window, "commit": "abc123"},
            "calendar": {"status": "verified"},
            "markets": {
                "taiwan": {"status": tw_status, "latestSessionDate": tw_latest,
                           "expectedSessionDate": tw_expected, "nextDueAt": tw_due},
                "us": {"status": "fresh", "latestSessionDate": "2026-09-28",
                       "expectedSessionDate": "2026-09-28", "nextDueAt": "2026-09-29T21:30:00+08:00"},
            },
            "sources": {"taiex": "ok", "vix": "ok", "006208": "ok"},
        }

    def test_v2_holiday_closure_is_healthy_when_latest_session_is_current(self):
        now = datetime.datetime(2026, 9, 29, 9, 23, tzinfo=TAIPEI)
        self.assertEqual(evaluate_status("Skynet Monitoring", self._skynet_v2(), now), [])

    def test_v2_replays_service_outage_even_when_holiday_data_is_valid(self):
        now = datetime.datetime(2026, 9, 29, 9, 23, tzinfo=TAIPEI)
        payload = self._skynet_v2(generated="2026-09-28T07:56:00+08:00", window_date="2026-09-28")
        issues = evaluate_status("Skynet Monitoring", payload, now)
        stale_issue = next(issue for issue in issues if "SERVICE_STALE" in issue)
        self.assertRegex(stale_issue, r"25\.4[0-9]?h|25\.5h")
        self.assertTrue(any("UPDATE_WINDOW_MISSED" in issue for issue in issues))
        self.assertFalse(any("Taiwan MARKET_DATA_STALE" in issue for issue in issues))

    def test_v2_market_stuck_after_close_buffer_is_stale(self):
        now = datetime.datetime(2026, 9, 29, 14, 31, tzinfo=TAIPEI)
        payload = self._skynet_v2(
            generated="2026-09-29T14:00:00+08:00", window="morning",
            tw_due="2026-09-29T14:30:00+08:00",
        )
        issues = evaluate_status("Skynet Monitoring", payload, now)
        self.assertTrue(any("Taiwan MARKET_DATA_STALE" in issue for issue in issues))

    def test_v2_calendar_failure_is_not_holiday_exempt(self):
        payload = self._skynet_v2()
        payload["calendar"]["status"] = "unavailable"
        issues = evaluate_status("Skynet Monitoring", payload, self.now)
        self.assertTrue(any("CALENDAR_UNVERIFIED" in issue for issue in issues))

    def test_v2_degraded_detail_is_not_duplicated_by_generic_status(self):
        payload = self._skynet_v2()
        payload["status"] = "degraded"
        payload["service"]["status"] = "degraded"
        payload["markets"]["us"]["status"] = "unavailable"
        issues = evaluate_status("Skynet Monitoring", payload, self.now)
        self.assertFalse(any("status=degraded" in issue for issue in issues))
        self.assertEqual(sum("US SOURCE_UNAVAILABLE" in issue for issue in issues), 1)

    def test_dry_run_suppresses_telegram_even_when_issues_exist(self):
        with patch("health_check.fetch_status", return_value=["Skynet SERVICE_STALE"]), \
             patch("health_check.send_alert") as send_alert, \
             patch.dict("os.environ", {"HEALTH_CHECK_DRY_RUN": "true"}, clear=False):
            self.assertEqual(__import__("health_check").main(), 1)
        send_alert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
