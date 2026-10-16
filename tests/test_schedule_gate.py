import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch

from schedule_gate import _stage_evidence_for_run, decide_fallback, dispatch_context, main, scheduled_context
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
            commit="abc", runs=runs, health_status="PASS",
        )
        self.assertEqual(result["decision"], "SKIP")
        self.assertEqual(result["reasonCode"], "SKIP_ALREADY_SUCCEEDED")
        self.assertEqual(result["window"], "us")

    def test_successful_workflow_without_private_health_marker_does_not_skip(self):
        runs = [{
            "status": "completed", "conclusion": "success", "headSha": "abc",
            "createdAt": "2026-09-15T21:42:00Z", "event": "repository_dispatch",
        }]
        result = decide_fallback(
            event_name="schedule", schedule="20 22 * * 1-5", now_utc=self.now,
            commit="abc", runs=runs,
        )
        self.assertEqual(result["decision"], "RUN")
        self.assertEqual(result["reasonCode"], "RUN_HEALTH_UNVERIFIED")

    def test_completed_settlement_with_unhealthy_risk_skips_fallback(self):
        runs = [{
            "status": "completed", "conclusion": "success", "headSha": "abc",
            "createdAt": "2026-09-15T21:42:00Z", "event": "repository_dispatch",
        }]
        result = decide_fallback(
            event_name="schedule", schedule="20 22 * * 1-5", now_utc=self.now,
            commit="abc", runs=runs, health_status="COMPLETE",
        )
        self.assertEqual(result["decision"], "SKIP")
        self.assertEqual(result["reasonCode"], "SKIP_ALREADY_SUCCEEDED")

    def test_data_unhealthy_run_does_not_skip_fallback(self):
        runs = [{
            "status": "completed", "conclusion": "success", "headSha": "abc",
            "createdAt": "2026-09-15T21:42:00Z", "event": "repository_dispatch",
        }]
        result = decide_fallback(
            event_name="schedule", schedule="20 22 * * 1-5", now_utc=self.now,
            commit="abc", runs=runs, health_status="UNHEALTHY",
        )
        self.assertEqual(result["decision"], "RUN")
        self.assertEqual(result["reasonCode"], "RUN_DATA_UNHEALTHY")

    def test_failed_primary_with_all_required_stage_proofs_skips_fallback(self):
        runs = [{
            "status": "completed", "conclusion": "failure", "headSha": "old",
            "createdAt": "2026-09-15T21:42:00Z", "event": "repository_dispatch", "databaseId": 123,
        }]
        result = decide_fallback(
            event_name="schedule", schedule="20 22 * * 1-5", now_utc=self.now,
            commit="new", runs=runs,
            stage_evidence={"settlementStatus": "COMPLETE", "notificationStatus": "SENT", "publicationStatus": "VERIFIED"},
            source_run_id="123",
        )
        self.assertEqual(result["decision"], "SKIP")
        self.assertEqual(result["reasonCode"], "SKIP_REQUIRED_STAGES_COMPLETE")

    def test_settlement_complete_with_only_notification_missing_selects_recovery(self):
        runs = [{
            "status": "completed", "conclusion": "failure", "headSha": "old",
            "createdAt": "2026-09-15T21:42:00Z", "event": "repository_dispatch", "databaseId": 123,
        }]
        result = decide_fallback(
            event_name="schedule", schedule="20 22 * * 1-5", now_utc=self.now,
            commit="new", runs=runs,
            stage_evidence={"settlementStatus": "COMPLETE", "notificationStatus": "FAILED", "publicationStatus": "VERIFIED"},
            source_run_id="123",
        )
        self.assertEqual(result["decision"], "RUN")
        self.assertEqual(result["recoveryMode"], "notification_only")
        self.assertEqual(result["sourceRunId"], "123")

    def test_recovered_notification_with_publication_missing_selects_publication_only(self):
        runs = [{
            "status": "completed", "conclusion": "failure", "headSha": "old",
            "createdAt": "2026-09-15T21:42:00Z", "event": "repository_dispatch", "databaseId": 123,
        }]
        result = decide_fallback(
            event_name="schedule", schedule="20 22 * * 1-5", now_utc=self.now,
            commit="new", runs=runs,
            stage_evidence={"settlementStatus": "COMPLETE", "notificationStatus": "FAILED", "publicationStatus": "FAILED"},
            recovery_evidence={"status": "SENT", "sourceRunId": "123"},
            source_run_id="123",
        )
        self.assertEqual(result["recoveryMode"], "publication_only")

    def test_missing_publication_artifact_keeps_settlement_and_notification_evidence(self):
        run = {"databaseId": 123, "headSha": "abc"}
        health = {"windowDate": "2026-09-15", "window": "tw", "settlementStatus": "COMPLETE"}
        notification = {"windowDate": "2026-09-15", "window": "tw", "status": "SENT"}
        with patch("schedule_gate._download_json_artifact", side_effect=[health, notification, None]):
            evidence = _stage_evidence_for_run(run, expected_window="tw", expected_date="2026-09-15")
        self.assertEqual(evidence["settlementStatus"], "COMPLETE")
        self.assertEqual(evidence["notificationStatus"], "SENT")
        self.assertEqual(evidence["publicationStatus"], "UNVERIFIED")
        result = decide_fallback(
            event_name="schedule", schedule="25 7 * * 1-5", now_utc=self.now,
            runs=[{
                "status": "completed", "conclusion": "failure", "headSha": "abc",
                "createdAt": "2026-09-15T06:45:00Z", "event": "repository_dispatch", "databaseId": 123,
            }],
            stage_evidence=evidence, source_run_id="123",
        )
        self.assertEqual(result["recoveryMode"], "publication_only")

    def test_explicitly_disabled_notification_is_not_auto_recovered(self):
        runs = [{
            "status": "completed", "conclusion": "success", "headSha": "abc",
            "createdAt": "2026-09-15T06:45:00Z", "event": "repository_dispatch", "databaseId": 123,
        }]
        result = decide_fallback(
            event_name="schedule", schedule="25 7 * * 1-5", now_utc=self.now,
            runs=runs,
            stage_evidence={"settlementStatus": "COMPLETE", "notificationStatus": "DISABLED", "publicationStatus": "VERIFIED"},
            source_run_id="123",
        )
        self.assertEqual(result["decision"], "SKIP")
        self.assertEqual(result["reasonCode"], "SKIP_REQUIRED_STAGES_COMPLETE")

    def test_verified_newer_publication_prevents_republishing_superseded_window(self):
        runs = [{
            "status": "completed", "conclusion": "failure", "headSha": "abc",
            "createdAt": "2026-09-15T06:45:00Z", "event": "repository_dispatch", "databaseId": 123,
        }]
        result = decide_fallback(
            event_name="schedule", schedule="25 7 * * 1-5", now_utc=self.now,
            runs=runs,
            stage_evidence={"settlementStatus": "COMPLETE", "notificationStatus": "SENT", "publicationStatus": "SUPERSEDED"},
            source_run_id="123",
        )
        self.assertEqual(result["decision"], "SKIP")
        self.assertEqual(result["reasonCode"], "SKIP_REQUIRED_STAGES_COMPLETE")

    def test_blocked_notification_recovery_is_not_retried_by_later_fallback(self):
        runs = [{
            "status": "completed", "conclusion": "failure", "headSha": "abc",
            "createdAt": "2026-09-15T06:45:00Z", "event": "repository_dispatch", "databaseId": 123,
        }]
        common = {
            "event_name": "schedule", "schedule": "25 7 * * 1-5", "now_utc": self.now,
            "runs": runs,
            "stage_evidence": {"settlementStatus": "COMPLETE", "notificationStatus": "FAILED", "publicationStatus": "VERIFIED"},
            "recovery_evidence": {"status": "BLOCKED_UPSTREAM"}, "source_run_id": "123",
        }
        result = decide_fallback(**common)
        self.assertEqual(result["decision"], "SKIP")
        self.assertEqual(result["reasonCode"], "SKIP_NOTIFICATION_RECOVERY_BLOCKED_UPSTREAM")

        common["stage_evidence"] = {"settlementStatus": "COMPLETE", "notificationStatus": "FAILED", "publicationStatus": "UNVERIFIED"}
        result = decide_fallback(**common)
        self.assertEqual(result["recoveryMode"], "publication_only")

    def test_recovery_technical_failure_is_not_repeated_but_explicit_429_can_retry(self):
        runs = [{
            "status": "completed", "conclusion": "failure", "headSha": "abc",
            "createdAt": "2026-09-15T06:45:00Z", "event": "repository_dispatch", "databaseId": 123,
        }]
        args = {
            "event_name": "schedule", "schedule": "25 7 * * 1-5", "now_utc": self.now,
            "runs": runs,
            "stage_evidence": {"settlementStatus": "COMPLETE", "notificationStatus": "FAILED", "publicationStatus": "VERIFIED"},
            "source_run_id": "123",
        }
        args["recovery_evidence"] = {"status": "FAILED", "reasonCode": "OUTBOX_HASH_INVALID"}
        result = decide_fallback(**args)
        self.assertEqual(result["decision"], "SKIP")
        self.assertEqual(result["reasonCode"], "SKIP_NOTIFICATION_RECOVERY_FAILED")

        args["recovery_evidence"] = {"status": "FAILED", "reasonCode": "TELEGRAM_RATE_LIMITED"}
        result = decide_fallback(**args)
        self.assertEqual(result["recoveryMode"], "notification_only")

    def test_stage_artifacts_are_bound_to_exact_publication_manifest(self):
        run = {"databaseId": 123, "headSha": "a" * 40}
        health = {"windowDate": "2026-09-15", "window": "tw", "settlementStatus": "COMPLETE"}
        notification = {"windowDate": "2026-09-15", "window": "tw", "status": "SENT"}
        proof = {
            "status": "VERIFIED", "runId": "123", "sourceCommit": "a" * 40,
            "windowDate": "2026-09-15", "window": "tw", "publicationId": "owner/repo:123:1:1",
            "contentHash": "b" * 64,
        }
        with patch("schedule_gate._download_json_artifact", side_effect=[health, notification, proof]):
            evidence = _stage_evidence_for_run(run, expected_window="tw", expected_date="2026-09-15")
        self.assertEqual(evidence["publicationStatus"], "VERIFIED")
        proof["sourceCommit"] = "c" * 40
        with patch("schedule_gate._download_json_artifact", side_effect=[health, notification, proof]):
            evidence = _stage_evidence_for_run(run, expected_window="tw", expected_date="2026-09-15")
        self.assertEqual(evidence["publicationStatus"], "UNVERIFIED")

    def test_non_schedule_main_does_not_require_schedule_artifact_locals(self):
        previous_directory = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                with patch.dict(os.environ, {
                    "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_EVENT_SCHEDULE": "",
                    "GITHUB_SHA": "abc", "GITHUB_OUTPUT": "",
                }, clear=False):
                    self.assertEqual(main(), 0)
            finally:
                os.chdir(previous_directory)

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

    def test_dispatch_context_identifies_only_known_primary_windows(self):
        morning = dt.datetime(2026, 9, 15, 21, 40, tzinfo=UTC)  # 05:40 Taipei
        afternoon = dt.datetime(2026, 9, 15, 6, 45, tzinfo=UTC)  # 14:45 Taipei
        outside = dt.datetime(2026, 9, 15, 10, 0, tzinfo=UTC)  # 18:00 Taipei
        self.assertEqual(dispatch_context(morning), {"date": "2026-09-16", "window": "us"})
        self.assertEqual(dispatch_context(afternoon), {"date": "2026-09-15", "window": "tw"})
        self.assertIsNone(dispatch_context(outside)["window"])

    def test_delayed_cron_after_midnight_keeps_prior_settlement_date(self):
        delayed = dt.datetime(2026, 9, 16, 17, 10, tzinfo=UTC)  # 01:10 Thupei
        context = scheduled_context("20 22 * * 1-5", delayed)
        self.assertEqual(context, {"date": "2026-09-16", "window": "us"})


if __name__ == "__main__":
    unittest.main()
