from datetime import date, datetime, timezone
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import send_verified_settlement as entry


DAY = date(2026, 11, 24)
STAMP = "2026-11-24T14:04:00Z"
SHA = "a" * 40
RUN = "1234567"


def fixture():
    run = {"id": int(RUN), "workflow_id": entry.WORKFLOW_ID, "run_attempt": 1,
           "head_branch": "main", "event": "workflow_dispatch", "head_sha": SHA,
           "head_repository": {"full_name": entry.REPOSITORY}, "status": "completed", "conclusion": "success"}
    jobs = [{"name": "build", "conclusion": "success", "started_at": "2026-11-24T14:03:00Z",
             "completed_at": "2026-11-24T14:05:00Z"},
            {"name": "Independently verify live Pages content", "conclusion": "success"}]
    health = {"windowDate": str(DAY), "window": None, "executionStatus": "COMPLETED",
              "settlementStatus": "COMPLETE", "partialUpdate": False}
    manifest = {"repository": entry.REPOSITORY, "runId": RUN, "sourceRunId": RUN, "sourceRunAttempt": "1",
                "sourceCommit": SHA, "windowDate": str(DAY), "window": None, "generatedAt": STAMP,
                "publicationId": f"{entry.REPOSITORY}:{RUN}:1:1"}
    manifest["contentHash"] = hashlib.sha256(json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    verified = {"status": "VERIFIED", **{k: manifest[k] for k in (
        "runId", "sourceCommit", "windowDate", "window", "contentHash", "publicationId")}}
    snapshot = {"generated_at": STAMP, "payload": {"generatedAt": STAMP, "status": "ok", "ledgerAudit": {"status": "OK"},
                "portfolio": {"netAsset": 105_001.0,
                              "buyHold": {"light": {"code": "RED"}},
                              "history": {"dates": ["11-23", "11-24"], "nets": [100_000, 105_000.999]},
                              "performance": {"available": True, "reconciled": True,
                                              "netChange": 5001.0, "netChangePercent": 5.0}}}}
    return run, jobs, health, verified, manifest, snapshot


class VerifiedSettlementTest(unittest.TestCase):
    def test_validates_source_and_formats_exact_unrounded_inputs(self):
        *data, snapshot = fixture()
        stamp, sha = entry.validate_source(*data, source_run=RUN, target=DAY)
        self.assertEqual(sha, SHA)
        self.assertEqual(entry.saved_message(snapshot, stamp=stamp, target=DAY),
                         "✅ 11/24 結算完畢！\n🔴 厲害的阿洲，今天賺了 5,000 元 (+5.0%)")

    def test_refuses_incomplete_wrong_identity_or_unverified_source(self):
        for index, key, value in ((0, "run_attempt", 2), (2, "partialUpdate", True),
                                  (3, "status", "SUPERSEDED"), (4, "generatedAt", "2026-11-24T13:00:00Z")):
            with self.subTest(key=key):
                *data, _ = fixture()
                data[index][key] = value
                with self.assertRaises(entry.Blocked):
                    entry.validate_source(*data, source_run=RUN, target=DAY)

    def test_refuses_overwritten_snapshot_and_mismatched_saved_values(self):
        *data, snapshot = fixture()
        stamp, _ = entry.validate_source(*data, source_run=RUN, target=DAY)
        bad = deepcopy(snapshot)
        bad["generated_at"] = "2026-11-24T14:15:00Z"
        with self.assertRaisesRegex(entry.Blocked, "PRIVATE_SNAPSHOT_DOES_NOT_MATCH_SOURCE"):
            entry.saved_message(bad, stamp=stamp, target=DAY)
        bad = deepcopy(snapshot)
        bad["payload"]["portfolio"]["performance"]["netChangePercent"] = 12.1
        with self.assertRaisesRegex(entry.Blocked, "SAVED_PERFORMANCE_MISMATCH"):
            entry.saved_message(bad, stamp=stamp, target=DAY)

    def test_tw_receipt_short_circuits_and_uncertainty_prevents_delivery(self):
        def reader(_, window):
            self.assertEqual(window, "tw")
            return {"status": "SENT", "messageId": "42"}

        self.assertEqual(entry.verify_pending(reader, DAY)["messageId"], "42")
        for key, state in (("tw", "SENDING"), ("tw", "DELIVERY_UNKNOWN"), ("manual", "SENT")):
            with self.subTest(key=key, state=state), self.assertRaises(entry.Blocked):
                entry.verify_pending(lambda _, window: {"status": state if window == key else "PENDING"}, DAY)

    def test_date_guard_expires_at_midnight_in_taiwan(self):
        with self.assertRaisesRegex(entry.Blocked, "SETTLEMENT_DATE_EXPIRED"):
            entry._same_day(DAY, datetime(2026, 11, 24, 16, 0, tzinfo=timezone.utc))

    def test_plan_is_read_only_and_live_send_persists_one_tw_receipt(self):
        run, jobs, health, verified, manifest, snapshot = fixture()
        states = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for item, value in (("run.json", run), ("jobs.json", {"jobs": jobs}),
                                ("health/settlement-health.json", health),
                                ("verification/pages-publication-verification.json", verified),
                                ("site/publication.json", manifest)):
                path = root / item
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps(value), encoding="utf-8")
            args = argparse.Namespace(date=str(DAY), source_run=RUN, evidence=root, mode="plan")
            stamp = datetime(2026, 11, 24, 14, 6, tzinfo=timezone.utc)
            sheet = object()
            with (patch.object(entry, "datetime", wraps=datetime) as date_mock,
                  patch("recover_settlement_notification._history_sheet", return_value=sheet) as open_sheet,
                  patch("supabase_sync.load_private_snapshot", return_value=snapshot) as load_snapshot,
                  patch("settlement_notification_state.read_state", side_effect=lambda _s, d, w: states.get((d, w), {"status": "PENDING"})),
                  patch("settlement_notification_state.write_state", side_effect=lambda _s, d, w, v: states.update({(d, w): v})) as save,
                  patch("requests.post") as post,
                  patch.dict("os.environ", {"TELEGRAM_TOKEN": "fake", "TELEGRAM_CHAT_ID": "fake"})):
                date_mock.now.return_value = stamp
                self.assertEqual(entry.run(args)["status"], "READY_TO_SEND")
                open_sheet.assert_called_once_with(readonly=True)
                load_snapshot.assert_called_once()
                self.assertFalse(hasattr(load_snapshot.call_args.kwargs["session"], "post"))
                save.assert_not_called()
                post.assert_not_called()
                args.mode = "send"
                post.return_value.status_code = 200
                post.return_value.json.return_value = {"ok": True, "result": {"message_id": 42}}
                self.assertEqual(entry.run(args)["messageId"], "42")
                self.assertEqual(entry.run(args)["status"], "ALREADY_SENT")
                self.assertEqual(save.call_count, 2)
                self.assertEqual(states[(str(DAY), "tw")]["status"], "SENT")
                self.assertEqual(set(states), {(str(DAY), "tw")})
                post.assert_called_once()
                self.assertEqual(post.call_args.kwargs["json"]["text"].splitlines()[0], "✅ 11/24 結算完畢！")


if __name__ == "__main__":
    unittest.main()
