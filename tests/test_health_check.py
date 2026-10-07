import datetime
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from health_check import (
    DEFAULT_STALE_AFTER_HOURS,
    GROWTH_STALE_AFTER_HOURS,
    GROWTH_URL,
    TAIPEI,
    _fetch_publication_mismatch,
    alert_plan,
    evaluate_status,
    incident_key,
    load_incident_state,
    normalize_incident_state,
    parse_generated_at,
    publication_root_issue,
    _replace_published_snapshot_incidents,
    save_incident_state,
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

    def test_successful_primary_run_satisfies_window_before_fallback_time(self):
        now = datetime.datetime(2026, 9, 29, 8, 0, tzinfo=TAIPEI)
        payload = self._skynet_v2(generated="2026-09-29T06:40:00+08:00")
        self.assertEqual(evaluate_status("Skynet Monitoring", payload, now), [])

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

    def test_publication_mismatch_collapses_window_and_stale_impact_only_with_verified_run(self):
        now = datetime.datetime(2026, 10, 7, 8, 0, tzinfo=TAIPEI)
        payload = self._skynet_v2(
            generated="2026-10-06T14:00:00+08:00", window="afternoon", window_date="2026-10-06",
            tw_latest="2026-10-05", tw_expected="2026-10-06", tw_status="stale",
            tw_due="2026-10-06T14:30:00+08:00",
        )
        payload["service"]["runId"] = "100"
        manifest = self._publication_manifest(payload)
        verified_run = {
            "id": 101, "run_attempt": 1, "conclusion": "success",
            "run_started_at": "2026-10-06T22:00:00Z",  # Oct 7 06:00 Taipei
        }
        proof = publication_root_issue(payload, manifest, verified_run, now)
        self.assertEqual(proof["expectedRunId"], 101)
        issues = evaluate_status("Skynet Monitoring", payload, now)
        grouped = _replace_published_snapshot_incidents(issues, proof)
        self.assertEqual(sum("PUBLICATION_NOT_VISIBLE" in item for item in grouped), 1)
        self.assertFalse(any("UPDATE_WINDOW_MISSED" in item or "MARKET_DATA_STALE" in item for item in grouped))

    def test_publication_mismatch_without_newer_verified_run_keeps_distinct_causes(self):
        now = datetime.datetime(2026, 10, 7, 8, 0, tzinfo=TAIPEI)
        payload = self._skynet_v2(window_date="2026-10-06")
        payload["service"]["runId"] = "100"
        manifest = self._publication_manifest(payload)
        older = {"id": 99, "run_attempt": 1, "conclusion": "success", "run_started_at": "2026-10-06T22:00:00Z"}
        self.assertIsNone(publication_root_issue(payload, manifest, older, now))

    def test_publication_mismatch_rejects_tampered_manifest_hash(self):
        payload = self._skynet_v2(window_date="2026-10-06")
        payload["service"]["runId"] = "100"
        manifest = self._publication_manifest(payload)
        manifest["window"] = "afternoon"
        verified = {"id": 101, "run_attempt": 1, "conclusion": "success",
                    "run_started_at": "2026-10-06T22:00:00Z"}
        self.assertIsNone(publication_root_issue(
            payload, manifest, verified, datetime.datetime(2026, 10, 7, 8, 0, tzinfo=TAIPEI),
        ))

    def test_publication_mismatch_requires_live_critical_file_hashes(self):
        now = datetime.datetime(2026, 10, 7, 8, 0, tzinfo=TAIPEI)
        payload = self._skynet_v2(
            generated="2026-10-06T14:00:00+08:00", window="afternoon", window_date="2026-10-06",
            tw_latest="2026-10-05", tw_expected="2026-10-06", tw_status="stale",
        )
        payload["service"]["runId"] = "100"
        manifest = self._publication_manifest(payload)
        public_files = {
            "index.html": b"<!doctype html><title>Skynet</title>",
            "data.json": b"{\"safe\":true}",
            "status.json": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(),
        }
        hashes = {name: hashlib.sha256(value).hexdigest() for name, value in public_files.items()}
        manifest["files"] = hashes
        manifest["criticalFiles"] = dict(hashes)
        unsigned = dict(manifest)
        unsigned.pop("contentHash", None)
        manifest["contentHash"] = hashlib.sha256(json.dumps(
            unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

        class Response:
            def __init__(self, *, payload=None, content=b""):
                self._payload = payload
                self.content = content

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class Requests:
            def __init__(self, mismatch=False):
                self.mismatch = mismatch

            def get(self, url, **_kwargs):
                if "/publication.json?" in url:
                    return Response(payload=manifest)
                name = url.split("?")[0].rsplit("/", 1)[-1]
                content = public_files[name]
                if self.mismatch and name == "data.json":
                    content += b"stale"
                return Response(content=content)

        with patch("health_check._verified_publication_run", return_value={
            "id": 101, "run_attempt": 1, "conclusion": "success",
            "run_started_at": "2026-10-06T22:00:00Z",
        }):
            self.assertIsNotNone(_fetch_publication_mismatch(payload, now, Requests()))
            self.assertIsNone(_fetch_publication_mismatch(payload, now, Requests(mismatch=True)))

    @staticmethod
    def _publication_manifest(payload):
        service = payload["service"]
        file_hashes = {
            name: hashlib.sha256(("verified:" + name).encode("utf-8")).hexdigest()
            for name in ("index.html", "data.json", "status.json")
        }
        manifest = {
            "schemaVersion": 1,
            "publicationId": f"hanjhou2000716/skynet-monitoring:{service['runId']}:1:1",
            "publicationAttempt": 1,
            "repository": "hanjhou2000716/skynet-monitoring",
            "runId": service["runId"],
            "runAttempt": "1",
            "sourceCommit": service["commit"],
            "windowDate": service["windowDate"],
            "window": service["window"],
            "generatedAt": service["generatedAt"],
            "dataStatus": payload["status"],
            "files": file_hashes,
            "criticalFiles": dict(file_hashes),
        }
        manifest["contentHash"] = hashlib.sha256(json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return manifest

    def test_v2_reports_actionable_instrument_dates_without_duplicate_market_reason(self):
        payload = self._skynet_v2(
            generated="2026-10-01T10:22:18+08:00", window_date="2026-10-01",
            tw_latest="2026-09-29", tw_expected="2026-09-30", tw_status="stale",
        )
        payload["instruments"] = {
            "^TWII": {"status": "fresh", "latestSessionDate": "2026-09-30",
                      "expectedSessionDate": "2026-09-30", "reasonCode": "OK", "sourceAttempts": 1},
            "006208": {"status": "stale", "latestSessionDate": "2026-09-29",
                       "expectedSessionDate": "2026-09-30", "reasonCode": "SOURCE_LAGGING",
                       "sourceAttempts": 3},
        }
        payload["markets"]["us"]["nextDueAt"] = "2026-10-02T06:30:00+08:00"
        issues = evaluate_status(
            "Skynet Monitoring", payload,
            datetime.datetime(2026, 10, 1, 13, 52, tzinfo=TAIPEI),
        )
        self.assertEqual(len(issues), 1)
        self.assertIn("Taiwan MARKET_DATA_STALE", issues[0])
        self.assertIn("symbol=006208", issues[0])
        self.assertIn("actual=2026-09-29 expected=2026-09-30", issues[0])
        self.assertIn("reason=SOURCE_LAGGING", issues[0])
        self.assertIn("attempts=3", issues[0])
        self.assertEqual(incident_key(issues[0]), "Skynet Monitoring|MARKET_DATA_STALE|taiwan")

    def test_instrument_and_source_alias_are_reported_once_with_recovery_evidence(self):
        payload = self._skynet_v2()
        payload["markets"]["taiwan"] = {
            "status": "market_closed", "latestSessionDate": "2026-10-02",
            "expectedSessionDate": "2026-10-02", "nextDueAt": "2026-10-05T14:30:00+08:00",
        }
        payload["instruments"] = {
            "^TWII": {"status": "fresh", "latestSessionDate": "2026-10-02",
                      "expectedSessionDate": "2026-10-02", "reasonCode": "OK"},
            "006208": {
                "status": "unavailable", "latestSessionDate": None,
                "expectedSessionDate": "2026-10-02", "reasonCode": "SOURCES_UNAVAILABLE",
                "sourceAttempts": 7, "cacheStatus": "MISSING_OR_INVALID",
                "sourceResults": {
                    "TWSE": {"status": "UNAVAILABLE", "reasonCode": "SOURCE_HTTP_503", "attempts": 3},
                    "Yahoo": {"status": "UNAVAILABLE", "reasonCode": "SOURCE_TIMEOUT", "attempts": 3},
                },
            },
            "^VIX": {"status": "fresh", "latestSessionDate": "2026-10-01",
                     "expectedSessionDate": "2026-10-01", "reasonCode": "OK"},
        }
        payload["sources"]["006208"] = "unavailable:SOURCES_UNAVAILABLE"
        issues = evaluate_status("Skynet Monitoring", payload, self.now)
        taiwan = [issue for issue in issues if "Taiwan SOURCE_UNAVAILABLE" in issue]
        self.assertEqual(len(taiwan), 1)
        self.assertIn("TWSE=unavailable:SOURCE_HTTP_503", taiwan[0])
        self.assertIn("Yahoo=unavailable:SOURCE_TIMEOUT", taiwan[0])
        self.assertIn("cache=MISSING_OR_INVALID", taiwan[0])

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
            self.assertEqual(__import__("health_check").main(), 0)
        send_alert.assert_not_called()

    def test_detected_health_incident_is_successful_watchdog_execution(self):
        issue = "Skynet Monitoring Taiwan SOURCE_UNAVAILABLE: symbol=006208"
        with patch("health_check.fetch_status", return_value=[issue]), \
             patch("health_check.load_incident_state", return_value={"schemaVersion": 1, "active": {}}), \
             patch("health_check.save_incident_state"), \
             patch("health_check.send_alert") as send_alert:
            result = __import__("health_check").main(dry_run=False, now=self.now)
        self.assertEqual(result, 0)
        send_alert.assert_called_once_with([issue])

    def test_incident_key_excludes_changing_stale_age(self):
        first = incident_key("Skynet Monitoring SERVICE_STALE: service stale for 25.5h (limit 18h)")
        later = incident_key("Skynet Monitoring SERVICE_STALE: service stale for 52.6h (limit 18h)")
        self.assertEqual(first, later)
        self.assertEqual(first, "Skynet Monitoring|SERVICE_AVAILABILITY|service")

    def test_window_missed_and_service_stale_share_one_service_incident(self):
        stale = incident_key("Skynet Monitoring SERVICE_STALE: service stale for 25.5h (limit 18h)")
        missed = incident_key("Skynet Monitoring UPDATE_WINDOW_MISSED: expected 2026-09-29 morning update")
        self.assertEqual(stale, missed)

    def test_market_incidents_are_scoped_to_affected_market(self):
        taiwan = incident_key("Skynet Monitoring Taiwan MARKET_DATA_STALE: deadline passed")
        us = incident_key("Skynet Monitoring US MARKET_DATA_STALE: deadline passed")
        self.assertNotEqual(taiwan, us)
        self.assertEqual(taiwan, "Skynet Monitoring|MARKET_DATA_STALE|taiwan")

    def test_alert_plan_sends_first_occurrence_and_waits_24_hours(self):
        issue = "Skynet Monitoring SERVICE_STALE: service stale for 25.5h (limit 18h)"
        first_now = datetime.datetime(2026, 9, 29, 9, 23, tzinfo=TAIPEI)
        due, state = alert_plan([issue], {"schemaVersion": 1, "active": {}}, first_now)
        self.assertEqual(due, [issue])
        self.assertEqual(state["active"][incident_key(issue)]["lastAlertAt"], first_now.isoformat())

        repeated = "Skynet Monitoring SERVICE_STALE: service stale for 40.0h (limit 18h)"
        within_24h = first_now + datetime.timedelta(hours=23, minutes=59)
        due, next_state = alert_plan([repeated], state, within_24h)
        self.assertEqual(due, [])
        self.assertEqual(next_state["active"], state["active"])

        at_24h = first_now + datetime.timedelta(hours=24)
        due, next_state = alert_plan([repeated], state, at_24h)
        self.assertEqual(due, [repeated])
        self.assertEqual(next_state["active"][incident_key(issue)]["lastAlertAt"], at_24h.isoformat())

    def test_recovery_clears_incident_state(self):
        key = "Skynet Monitoring|SERVICE_AVAILABILITY|service"
        state = {"schemaVersion": 1, "active": {key: {
            "firstSeenAt": self.now.isoformat(), "lastAlertAt": self.now.isoformat(),
        }}}
        due, recovered = alert_plan([], state, self.now + datetime.timedelta(hours=1))
        self.assertEqual(due, [])
        self.assertEqual(recovered["active"], {})

    def test_corrupt_incident_state_fails_open(self):
        issue = "Growth Dashboard stale for 73.0h (limit 72h)"
        due, state = alert_plan([issue], {"schemaVersion": 999, "active": {}}, self.now)
        self.assertEqual(due, [issue])
        self.assertEqual(normalize_incident_state({"not": "a state"})["active"], {})

    def test_telegram_failure_does_not_advance_last_alert_marker(self):
        issue = "Growth Dashboard stale for 73.0h (limit 72h)"
        old = self.now - datetime.timedelta(hours=30)
        key = incident_key(issue)
        prior = {"schemaVersion": 1, "active": {key: {
            "firstSeenAt": old.isoformat(), "lastAlertAt": old.isoformat(),
        }}}
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            save_incident_state(prior, path)
            with patch("health_check.fetch_status", return_value=[issue]), \
                 patch("health_check.load_incident_state", return_value=prior), \
                 patch("health_check.send_alert", side_effect=RuntimeError("telegram unavailable")), \
                 patch.dict(os.environ, {"HEALTH_WATCHDOG_STATE_FILE": path}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "telegram unavailable"):
                    __import__("health_check").main(dry_run=False, now=self.now)
            with open(path, encoding="utf-8") as file:
                saved = json.load(file)
            self.assertEqual(saved["active"][key]["lastAlertAt"], old.isoformat())

    def test_healthy_run_clears_saved_incident_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            with patch("health_check.fetch_status", return_value=[]), \
                 patch.dict(os.environ, {"HEALTH_WATCHDOG_STATE_FILE": path}, clear=False):
                self.assertEqual(__import__("health_check").main(dry_run=False, now=self.now), 0)
            with open(path, encoding="utf-8") as file:
                state = json.load(file)
            self.assertEqual(state["active"], {})

    def test_loads_state_from_latest_completed_watchdog_artifact(self):
        key = "Skynet Monitoring|SERVICE_AVAILABILITY|service"
        expected = {"schemaVersion": 1, "active": {key: {
            "firstSeenAt": "2026-09-29T09:23:00+08:00",
            "lastAlertAt": "2026-09-29T09:23:00+08:00",
        }}}
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w") as archive:
            archive.writestr("health-watchdog-state.json", json.dumps(expected))

        class Response:
            def __init__(self, payload=None, content=b""):
                self.payload = payload
                self.content = content

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        responses = [
            Response({"workflow_runs": [{"id": 101}]}),
            Response({"artifacts": [{"name": "health-watchdog-state", "expired": False,
                                      "archive_download_url": "https://example.test/archive"}]}),
            Response(content=archive_buffer.getvalue()),
        ]
        with patch("requests.get", side_effect=responses):
            state = load_incident_state(environ={
                "GITHUB_TOKEN": "test-token", "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "202",
            })
        self.assertEqual(state, expected)

    def test_state_artifact_download_failure_fails_open(self):
        with patch("requests.get", side_effect=OSError("artifact unavailable")):
            state = load_incident_state(environ={
                "GITHUB_TOKEN": "test-token", "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "202",
            })
        self.assertEqual(state, {"schemaVersion": 1, "active": {}})

    def test_watchdog_dispatch_backup_queue_and_artifact_contract(self):
        workflow_path = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "health-watchdog.yml")
        with open(workflow_path, encoding="utf-8") as file:
            workflow = file.read()
        self.assertIn("repository_dispatch:", workflow)
        self.assertIn("types: [health_check]", workflow)
        self.assertIn('cron: "15 0 * * *"', workflow)
        self.assertIn('cron: "15 9 * * *"', workflow)
        self.assertIn("queue: max", workflow)
        self.assertIn("actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02", workflow)
        self.assertIn("always()", workflow)


if __name__ == "__main__":
    unittest.main()
