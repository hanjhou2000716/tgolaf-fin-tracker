import datetime as dt
import unittest

from schedule_gate import decide_fallback, scheduled_context
from service_contracts import UTC


class ScheduleGateTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 9, 15, 22, 25, tzinfo=UTC)  # 06:25 Tuepei

    def test_same_window_date_and_commit_skips(self):
        runs = [{
            "status": "completed", "conclusion": "success", "headSha": "abc",
            "createdAt": "2026-09-15T21:42:00Z", "event": "repository_dispatch",
        }]
        result = decide_fallback(
            event_name="schedule", schedule="20 22 * * 1-5", now_utc=self.now,
            commit="abc", runs=runs,
        )
        self.assertEqual(result["decision"], "SKIP")
        self.assertEqual(result["reasonCode"], "SKIP_ALREADY_SUCCEEDED")
        self.assertEqual(result["window"], "us")

    def test_old_date_window_or_commit_does_not_skip(self):
        runs = [{
            "status": "completed", "conclusion": "success", "headSha": "old",
            "createdAt": "2026-09-14T21:42:00Z", "event": "repository_dispatch",
        }]
        result = decide_fallback(
            event_name="schedule", schedule="20 22 * * 1-5", now_utc=self.now,
            commit="abc", runs=runs,
        )
        self.assertEqual(result["decision"], "RUN")
        self.assertEqual(result["reasonCode"], "RUN_NO_SUCCESSFUL_MATCH")

    def test_api_unavailable_runs_for_availability(self):
        result = decide_fallback(
            event_name="schedule", schedule="25 7 * * 1-5", now_utc=self.now,
            commit="abc", runs=(), api_error="timeout",
        )
        self.assertEqual(result["decision"], "RUN")
        self.assertEqual(result["reasonCode"], "RUN_API_UNAVAILABLE")

    def test_manual_and_dispatch_are_always_allowed(self):
        for event in ("workflow_dispatch", "repository_dispatch"):
            result = decide_fallback(event_name=event, schedule="", now_utc=self.now)
            self.assertEqual(result["decision"], "RUN")
            self.assertEqual(result["reasonCode"], "RUN_NON_SCHEDULE")

    def test_unknown_schedule_is_safe_run(self):
        result = decide_fallback(event_name="schedule", schedule="0 0 * * *", now_utc=self.now)
        self.assertEqual(result["decision"], "RUN")
        self.assertEqual(result["reasonCode"], "RUN_UNKNOWN_SCHEDULE")

    def test_context_uses_taipei_calendar(self):
        context = scheduled_context("25 7 * * 1-5", self.now)
        self.assertEqual(context, {"date": "2026-09-15", "window": "tw"})

    def test_delayed_cron_after_midnight_keeps_prior_settlement_date(self):
        delayed = dt.datetime(2026, 9, 16, 17, 10, tzinfo=UTC)  # 01:10 Thupei
        context = scheduled_context("20 22 * * 1-5", delayed)
        self.assertEqual(context, {"date": "2026-09-16", "window": "us"})


if __name__ == "__main__":
    unittest.main()
