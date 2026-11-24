"""Deliver one same-day, already completed Growth settlement without rerunning it."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timezone
import json
import math
import os
from pathlib import Path
import re

from buy_hold_policy import build_settlement_telegram_message
from pages_publication import _manifest_hash_is_valid
from service_contracts import GROWTH_BUTTON_TEXT, TAIPEI, rfc3339_utc


WORKFLOW_ID = 281279630
REPOSITORY = "hanjhou2000716/tgolaf-fin-tracker"
MINIAPP = "https://hanjhou2000716.github.io/tgolaf-fin-tracker/private/"


class Blocked(ValueError):
    """Known stop condition whose message is safe to record."""


def _object(value):
    if not isinstance(value, dict):
        raise Blocked("EVIDENCE_OBJECT_INVALID")
    return value


def _instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is not None:
            return parsed
    except (AttributeError, TypeError, ValueError, OverflowError):
        pass
    raise Blocked("EVIDENCE_TIMESTAMP_INVALID")


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise Blocked("SAVED_PERFORMANCE_UNAVAILABLE")
    return value


def _same_day(target, now):
    if now.astimezone(TAIPEI).date() != target:
        raise Blocked("SETTLEMENT_DATE_EXPIRED")


def validate_source(run, jobs, health, publication, manifest, *, source_run, target):
    day = target.isoformat()
    sha = run.get("head_sha")
    if (str(run.get("id")) != source_run or run.get("workflow_id") != WORKFLOW_ID
            or run.get("run_attempt") != 1 or run.get("head_branch") != "main"
            or run.get("event") not in {"workflow_dispatch", "repository_dispatch", "schedule"}
            or run.get("status") != "completed" or run.get("conclusion") != "success"
            or _object(run.get("head_repository")).get("full_name") != REPOSITORY
            or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha)):
        raise Blocked("SOURCE_RUN_NOT_TRUSTED")
    by_name = {job.get("name"): job for job in jobs if isinstance(job, dict)}
    if any(by_name.get(name, {}).get("conclusion") != "success"
           for name in ("build", "Independently verify live Pages content")):
        raise Blocked("SOURCE_JOBS_NOT_VERIFIED")
    if (health.get("windowDate") != day or health.get("window") not in (None, "tw")
            or health.get("executionStatus") != "COMPLETED"
            or health.get("settlementStatus") != "COMPLETE" or health.get("partialUpdate") is not False):
        raise Blocked("SOURCE_SETTLEMENT_INCOMPLETE")
    if (manifest.get("repository") != REPOSITORY or str(manifest.get("sourceRunId")) != source_run
            or str(manifest.get("sourceRunAttempt")) != "1" or str(manifest.get("runId")) != source_run
            or manifest.get("sourceCommit") != sha or manifest.get("windowDate") != day
            or manifest.get("window") not in (None, "tw") or not _manifest_hash_is_valid(manifest)
            or publication.get("status") != "VERIFIED"
            or any(publication.get(key) != manifest.get(key) for key in (
                "runId", "sourceCommit", "windowDate", "window", "contentHash", "publicationId"))):
        raise Blocked("SOURCE_PUBLICATION_NOT_VERIFIED")
    stamp = _instant(manifest.get("generatedAt"))
    local = stamp.astimezone(TAIPEI)
    build = by_name["build"]
    if (local.date() != target or local.time() < time(14, 0)
            or not _instant(build.get("started_at")) <= stamp <= _instant(build.get("completed_at"))):
        raise Blocked("SOURCE_NOT_AFTERNOON_RESULT")
    return stamp, sha


def saved_message(record, *, stamp, target):
    record = _object(record)
    payload = _object(record.get("payload"))
    if (_instant(record.get("generated_at")) != stamp or _instant(payload.get("generatedAt")) != stamp
            or payload.get("status") != "ok" or _object(payload.get("ledgerAudit")).get("status") != "OK"):
        raise Blocked("PRIVATE_SNAPSHOT_DOES_NOT_MATCH_SOURCE")
    portfolio = _object(payload.get("portfolio"))
    performance = _object(portfolio.get("performance"))
    history = _object(portfolio.get("history"))
    days, nets = history.get("dates"), history.get("nets")
    if (performance.get("available") is not True or performance.get("reconciled") is not True
            or not isinstance(days, list) or not isinstance(nets, list) or len(days) != len(nets)
            or len(days) < 2 or days[-1] != target.strftime("%m-%d") or days[-2] == days[-1]):
        raise Blocked("SAVED_PERFORMANCE_UNAVAILABLE")
    current, previous = _number(nets[-1]), _number(nets[-2])
    if previous <= 0 or current <= 0:
        raise Blocked("SAVED_PERFORMANCE_UNAVAILABLE")
    # Recover the exact rendering inputs from the recorded history; check that
    # they match the snapshot's stored P&L. No transactions/prices are replayed.
    difference = current - previous
    percent = difference / previous * 100
    if (abs(round(difference, 2) - _number(performance.get("netChange"))) > 0.0001
            or abs(round(percent, 2) - _number(performance.get("netChangePercent"))) > 0.0001
            or abs(round(current, 2) - _number(portfolio.get("netAsset"))) > 0.0001):
        raise Blocked("SAVED_PERFORMANCE_MISMATCH")
    policy = _object(portfolio.get("buyHold"))
    _object(policy.get("light"))
    return build_settlement_telegram_message(target.strftime("%m/%d"), difference, percent, policy)


def verify_pending(reader, target):
    tw = reader(target.isoformat(), "tw")
    if tw.get("status") == "SENT":
        return {"status": "ALREADY_SENT", "messageId": tw.get("messageId"), "httpAttempted": False}
    if tw.get("status") != "PENDING" or reader(target.isoformat(), "manual").get("status") != "PENDING":
        raise Blocked("PRIOR_DELIVERY_NOT_PROVEN_ABSENT")
    return None


def _read(path):
    return _object(json.loads(path.read_text(encoding="utf-8")))


def run(args):
    import requests

    from recover_settlement_notification import _history_sheet
    from settlement_notification_state import read_state, write_state
    from supabase_sync import load_private_snapshot
    from telegram_delivery import deliver_once

    now = datetime.now(timezone.utc)
    target = date.fromisoformat(args.date)
    _same_day(target, now)
    evidence = args.evidence
    stamp, sha = validate_source(
        _read(evidence / "run.json"), _read(evidence / "jobs.json").get("jobs") or [],
        _read(evidence / "health" / "settlement-health.json"),
        _read(evidence / "verification" / "pages-publication-verification.json"),
        _read(evidence / "site" / "publication.json"), source_run=args.source_run, target=target,
    )
    sheet = _history_sheet(readonly=args.mode == "plan")
    reader = lambda day, window: read_state(sheet, day, window)
    prior = verify_pending(reader, target)
    if prior is not None:
        return prior

    class ReadOnlySnapshot:
        @staticmethod
        def get(url, **kwargs):
            return requests.get(url, allow_redirects=False, **kwargs)

    message = saved_message(load_private_snapshot(session=ReadOnlySnapshot()), stamp=stamp, target=target)
    if args.mode == "plan":
        return {"status": "READY_TO_SEND", "httpAttempted": False}
    token, chat_id = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise Blocked("TELEGRAM_CREDENTIALS_MISSING")
    _same_day(target, datetime.now(timezone.utc))

    def post(url, **kwargs):
        _same_day(target, datetime.now(timezone.utc))
        return requests.post(url, allow_redirects=False, **kwargs)

    return deliver_once(
        date_key=target.isoformat(), window="tw", token=token,
        payload={"chat_id": chat_id, "text": message, "parse_mode": "Markdown", "reply_markup": {
            "inline_keyboard": [[{"text": GROWTH_BUTTON_TEXT, "web_app": {"url": MINIAPP}}]],
        }},
        load_state=lambda: reader(target.isoformat(), "tw"),
        save_state=lambda state: write_state(sheet, target.isoformat(), "tw", state),
        post=post, now=rfc3339_utc(datetime.now(timezone.utc)), source_metadata={
            "sourceRunId": args.source_run, "sourceCommit": sha,
            "notificationType": "settlement", "messageKind": "complete",
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("plan", "send"), default="plan")
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = run(args)
    except Blocked as error:
        result = {"status": "BLOCKED", "reasonCode": str(error), "httpAttempted": False}
    except Exception as error:
        # Never print HTTP URLs, service responses, snapshots, or the message.
        result = {"status": "FAILED", "reasonCode": getattr(error, "reason_code", type(error).__name__)}
    public_fields = ("status", "reasonCode", "messageId", "sentAt", "attemptId", "httpAttempted")
    safe = {key: result[key] for key in public_fields if key in result}
    safe.update({"sourceRunId": args.source_run, "windowDate": args.date, "window": "tw"})
    print(json.dumps(safe, ensure_ascii=False, sort_keys=True))
    return 0 if safe["status"] in {"READY_TO_SEND", "ALREADY_SENT", "SENT"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
