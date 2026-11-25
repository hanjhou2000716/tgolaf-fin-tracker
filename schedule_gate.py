"""Conditional GitHub fallback gate for the two Taiwan settlement windows.

The gate is deliberately non-financial: it only compares trigger metadata,
Taiwan dates/windows and commit SHAs.  It never reads portfolio or Telegram
payloads.  A failure to query GitHub is availability-first and therefore RUN.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

from service_contracts import TAIPEI, UTC


FALLBACK_SCHEDULES = {
    "20 22 * * 1-5": "us",  # 06:20 Tue-Sat Taiwan
    "25 7 * * 1-5": "tw",  # 15:25 Mon-Fri Taiwan
}
WINDOW_BOUNDS = {
    "us": (dt.time(5, 0), dt.time(6, 20)),
    "tw": (dt.time(14, 0), dt.time(15, 25)),
}


def _parse_time(value: Any) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(TAIPEI)


def scheduled_context(schedule: str, now_utc: dt.datetime | None = None) -> dict[str, str | None]:
    """Return the intended local date/window for a fallback invocation."""
    now = (now_utc or dt.datetime.now(UTC))
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local = now.astimezone(TAIPEI)
    window = FALLBACK_SCHEDULES.get(str(schedule or "").strip())
    # GitHub may deliver a scheduled event after midnight.  Keep the date of
    # the intended settlement session instead of letting the delayed start
    # create a new History/notification date key.
    if window == "us" and local.time() < dt.time(5, 0):
        local = local - dt.timedelta(days=1)
    elif window == "tw" and local.time() < dt.time(14, 0):
        local = local - dt.timedelta(days=1)
    return {
        "date": local.date().isoformat(),
        "window": window,
    }


def dispatch_context(now_utc: dt.datetime | None = None) -> dict[str, str | None]:
    """Tag primary repository_dispatch runs only inside known settlement windows."""
    now = now_utc or dt.datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local = now.astimezone(TAIPEI)
    clock = local.time().replace(tzinfo=None)
    if WINDOW_BOUNDS["us"][0] <= clock < WINDOW_BOUNDS["us"][1]:
        window = "us"
    elif WINDOW_BOUNDS["tw"][0] <= clock < WINDOW_BOUNDS["tw"][1]:
        window = "tw"
    else:
        window = None
    return {"date": local.date().isoformat(), "window": window}


def _successful_run_matches(
    runs: Iterable[dict[str, Any]], *, window: str, snapshot_date: str, commit: str
) -> bool:
    """Check only successful same-date, same-window, same-SHA runs."""
    start, end = WINDOW_BOUNDS[window]
    for run in runs:
        if str(run.get("status") or "").lower() != "completed":
            continue
        if str(run.get("conclusion") or "").lower() != "success":
            continue
        if commit and str(run.get("headSha") or "") != commit:
            continue
        created = run.get("createdAt") or run.get("runStartedAt")
        if not created:
            continue
        try:
            local = _parse_time(created)
        except (TypeError, ValueError, OverflowError):
            continue
        if local.date().isoformat() != snapshot_date:
            continue
        if start <= local.time().replace(tzinfo=None) < end:
            return True
    return False


def _matching_successful_run(
    runs: Iterable[dict[str, Any]], *, window: str, snapshot_date: str, commit: str
) -> dict[str, Any] | None:
    start, end = WINDOW_BOUNDS[window]
    candidates = []
    for run in runs:
        if str(run.get("status") or "").lower() != "completed" or str(run.get("conclusion") or "").lower() != "success":
            continue
        if commit and str(run.get("headSha") or "") != commit:
            continue
        created = run.get("createdAt") or run.get("runStartedAt")
        if not created:
            continue
        try:
            local = _parse_time(created)
        except (TypeError, ValueError, OverflowError):
            continue
        if local.date().isoformat() == snapshot_date and start <= local.time().replace(tzinfo=None) < end:
            candidates.append((local, run))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1] if candidates else None


def _matching_primary_run(
    runs: Iterable[dict[str, Any]], *, window: str, snapshot_date: str
) -> dict[str, Any] | None:
    """Find the primary trigger for a window even if a later stage failed."""
    start, end = WINDOW_BOUNDS[window]
    candidates = []
    for run in runs:
        if str(run.get("status") or "").lower() != "completed":
            continue
        if str(run.get("event") or "") != "repository_dispatch":
            continue
        created = run.get("createdAt") or run.get("runStartedAt")
        if not created:
            continue
        try:
            local = _parse_time(created)
        except (TypeError, ValueError, OverflowError):
            continue
        if local.date().isoformat() == snapshot_date and start <= local.time().replace(tzinfo=None) < end:
            candidates.append((local, run))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1] if candidates else None


def decide_fallback(
    *,
    event_name: str,
    schedule: str,
    now_utc: dt.datetime | None = None,
    commit: str = "",
    runs: Iterable[dict[str, Any]] | None = None,
    api_error: str | None = None,
    health_status: str | None = None,
    stage_evidence: dict[str, Any] | None = None,
    recovery_evidence: dict[str, Any] | None = None,
    source_run_id: str | None = None,
) -> dict[str, Any]:
    """Make a deterministic RUN/SKIP decision with a safe reason code."""
    context = scheduled_context(schedule, now_utc)
    window = context["window"]
    result = {
        "date": context["date"],
        "window": window,
        "trigger": event_name or "unknown",
        "commit": commit,
        "foundSuccessfulRun": False,
        "decision": "RUN",
        "reasonCode": "RUN_NON_SCHEDULE",
        "recoveryMode": "settlement",
        "sourceRunId": source_run_id,
    }
    if event_name != "schedule":
        return result
    if not window:
        result["reasonCode"] = "RUN_UNKNOWN_SCHEDULE"
        return result
    if api_error:
        result["reasonCode"] = "RUN_API_UNAVAILABLE"
        return result
    match = _matching_primary_run(runs or (), window=window, snapshot_date=str(context["date"]))
    if match and stage_evidence:
        settlement = str(stage_evidence.get("settlementStatus") or "").upper()
        notification = str(stage_evidence.get("notificationStatus") or "").upper()
        publication = str(stage_evidence.get("publicationStatus") or "").upper()
        recovered_notification = str((recovery_evidence or {}).get("status") or "").upper()
        recovered_publication = str((recovery_evidence or {}).get("publicationStatus") or "").upper()
        recovery_reason = str((recovery_evidence or {}).get("reasonCode") or "").upper()
        publication_complete = publication in {"VERIFIED", "SUPERSEDED"} or recovered_publication in {"VERIFIED", "SUPERSEDED"}
        # DISABLED is an explicit operator choice, not a recoverable send
        # failure.  In particular, a later scheduled fallback must never turn
        # a notification-disabled validation run into a real Telegram send.
        notification_complete = notification in {"SENT", "SUPPRESSED", "DISABLED", "NOT_REQUIRED"} or recovered_notification in {"SENT", "SUPPRESSED", "ALREADY_SENT", "SOURCE_RECEIPT_ALREADY_CONFIRMED"}
        notification_recovery_blocked = (
            recovered_notification in {"BLOCKED_UPSTREAM", "DELIVERY_UNKNOWN", "EXPIRED"}
            or (recovered_notification == "FAILED" and recovery_reason != "TELEGRAM_RATE_LIMITED")
        )
        if settlement == "COMPLETE" and notification_complete and publication_complete:
            result.update(foundSuccessfulRun=True, decision="SKIP", reasonCode="SKIP_REQUIRED_STAGES_COMPLETE")
            return result
        if settlement == "COMPLETE":
            if notification_recovery_blocked:
                if publication_complete:
                    result.update(
                        foundSuccessfulRun=True, decision="SKIP",
                        reasonCode=f"SKIP_NOTIFICATION_RECOVERY_{recovered_notification}",
                    )
                    return result
                mode, reason = "publication_only", "RUN_PUBLICATION_ONLY_NOTIFICATION_RECOVERY_BLOCKED"
            elif not notification_complete and not publication_complete:
                mode, reason = "notification_and_publication", "RUN_NOTIFICATION_AND_PUBLICATION_ONLY"
            elif not notification_complete:
                mode, reason = "notification_only", "RUN_NOTIFICATION_ONLY"
            elif not publication_complete:
                mode, reason = "publication_only", "RUN_PUBLICATION_ONLY"
            else:
                mode, reason = "settlement", "RUN_STAGE_EVIDENCE_INCONSISTENT"
            if mode != "settlement":
                result.update(
                    foundSuccessfulRun=True, decision="RUN", reasonCode=reason,
                    recoveryMode=mode, sourceRunId=str(source_run_id or match.get("databaseId") or ""),
                )
                return result
            result.update(foundSuccessfulRun=True, reasonCode=reason)
            return result
        result.update(foundSuccessfulRun=True, reasonCode="RUN_SETTLEMENT_INCOMPLETE")
        return result

    # Without settlement or notification evidence, retain the legacy rule:
    # only a globally successful same-commit run may prove completion.
    match = _matching_successful_run(runs or (), window=window, snapshot_date=str(context["date"]), commit=commit)
    stage_status = str(health_status or "").upper()
    if match and stage_status in {"PASS", "COMPLETE"}:
        result["foundSuccessfulRun"] = True
        result["decision"] = "SKIP"
        result["reasonCode"] = "SKIP_ALREADY_SUCCEEDED"
    elif match:
        result["foundSuccessfulRun"] = True
        result["reasonCode"] = (
            "RUN_HEALTH_UNVERIFIED" if not stage_status
            else "RUN_SETTLEMENT_INCOMPLETE" if stage_status == "INCOMPLETE"
            else "RUN_DATA_UNHEALTHY"
        )
    else:
        result["reasonCode"] = "RUN_NO_SUCCESSFUL_MATCH"
    return result


def _query_runs() -> list[dict[str, Any]]:
    repository = os.getenv("GITHUB_REPOSITORY", "")
    workflow = os.getenv("GITHUB_WORKFLOW_REF", "").split("@", 1)[0].rsplit("/", 1)[-1] or "cron.yml"
    command = [
        "gh", "run", "list", "--repo", repository, "--workflow", workflow,
        "--branch", "main", "--limit", "50", "--json",
        "status,conclusion,headSha,createdAt,event,databaseId,attempt",
    ]
    completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    value = json.loads(completed.stdout or "[]")
    return value if isinstance(value, list) else []


def _health_status_for_run(run: dict[str, Any] | None, *, expected_window: str | None, expected_date: str | None) -> str:
    if not run or not run.get("databaseId"):
        return "UNVERIFIED"
    repository = os.getenv("GITHUB_REPOSITORY", "")
    with tempfile.TemporaryDirectory(prefix="growth-health-gate-") as tmp:
        command = [
            "gh", "run", "download", str(run["databaseId"]), "--repo", repository,
            "--name", "settlement-health", "--dir", tmp,
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=45)
            marker = json.loads((Path(tmp) / "settlement-health.json").read_text(encoding="utf-8"))
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            return "UNVERIFIED"
    if not isinstance(marker, dict):
        return "UNVERIFIED"
    if marker.get("window") != expected_window or marker.get("windowDate") != expected_date:
        return "UNVERIFIED"
    # New markers gate retries on completed settlement/notification stages.
    # Legacy markers remain fail-closed: only healthStatus=PASS can skip.
    return str(marker.get("completionStatus", marker.get("healthStatus", "UNVERIFIED"))).upper()


def _download_json_artifact(run_id: Any, artifact_name: str, filename: str) -> dict[str, Any] | None:
    repository = os.getenv("GITHUB_REPOSITORY", "")
    with tempfile.TemporaryDirectory(prefix="growth-stage-evidence-") as tmp:
        command = ["gh", "run", "download", str(run_id), "--repo", repository, "--name", artifact_name, "--dir", tmp]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=45)
            value = json.loads((Path(tmp) / filename).read_text(encoding="utf-8"))
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            return None
    return value if isinstance(value, dict) else None


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value.lower())


def _source_order(run_id: Any, attempt: Any) -> tuple[int, int] | None:
    try:
        parsed = int(str(run_id)), int(str(attempt))
    except (TypeError, ValueError):
        return None
    return parsed if parsed[0] > 0 and parsed[1] > 0 else None


def _publication_proof_status(
    proof: dict[str, Any] | None,
    *,
    expected_source_run_id: Any,
    expected_source_run_attempt: Any,
    expected_commit: str,
    expected_date: str,
    expected_window: str,
    proof_run_id: Any | None = None,
    proof_run_attempt: Any | None = None,
    recovery: bool = False,
) -> str:
    """Return only publication states backed by a matching live readback proof."""
    if not isinstance(proof, dict):
        return "UNVERIFIED"
    expected_run = str(expected_source_run_id or "")
    expected_attempt = str(expected_source_run_attempt or "")
    if (not expected_run or proof.get("sourceCommit") != expected_commit
            or proof.get("windowDate") != expected_date or proof.get("window") != expected_window):
        return "UNVERIFIED"
    if proof.get("sourceRunId") is not None and str(proof.get("sourceRunId")) != expected_run:
        return "UNVERIFIED"
    if proof.get("sourceRunAttempt") is not None and expected_attempt and str(proof.get("sourceRunAttempt")) != expected_attempt:
        return "UNVERIFIED"

    publication_id = str(proof.get("publicationId") or "")
    if not publication_id or not _valid_sha256(proof.get("contentHash")):
        return "UNVERIFIED"

    if recovery:
        if proof_run_id is None or str(proof.get("runId") or "") != str(proof_run_id):
            return "UNVERIFIED"
        if proof_run_attempt is not None and str(proof.get("runAttempt") or "") != str(proof_run_attempt):
            return "UNVERIFIED"
        source_identity = _source_order(expected_run, expected_attempt)
        recovery_identity = _source_order(proof.get("runId"), proof.get("runAttempt"))
        if recovery_identity is None or f":{recovery_identity[0]}:{recovery_identity[1]}:" not in publication_id:
            return "UNVERIFIED"
        origin_id = str(proof.get("originPublicationId") or "")
        if (source_identity is None or f":{source_identity[0]}:{source_identity[1]}:" not in origin_id
                or not _valid_sha256(proof.get("originContentHash"))
                or not _valid_sha256(proof.get("filesHash"))):
            return "UNVERIFIED"

    status = str(proof.get("status") or "").upper()
    try:
        schema_version = int(proof.get("schemaVersion") or 1)
    except (TypeError, ValueError):
        return "UNVERIFIED"
    if status == "VERIFIED":
        # Legacy summaries had no observed-manifest fields. They remain
        # compatible only when the verifier says the exact candidate ID was live.
        if schema_version < 2:
            if not recovery and str(proof.get("runId") or "") == expected_run:
                if expected_attempt and f":{expected_run}:{expected_attempt}:" not in publication_id:
                    return "UNVERIFIED"
                return "VERIFIED"
            return "UNVERIFIED"
        observed_id = str(proof.get("observedPublicationId") or "")
        if (proof.get("observedStatus") != "VERIFIED" or not observed_id
                or not _valid_sha256(proof.get("observedContentHash"))
                or not _valid_sha256(proof.get("observedFilesHash"))):
            return "UNVERIFIED"
        same_source = (
            str(proof.get("observedSourceRunId") or "") == expected_run
            and (not expected_attempt or str(proof.get("observedSourceRunAttempt") or "") == expected_attempt)
            and proof.get("observedSourceCommit") == expected_commit
            and proof.get("observedWindowDate") == expected_date
            and proof.get("observedWindow") == expected_window
            and proof.get("observedFilesHash") == proof.get("filesHash")
        )
        if observed_id == publication_id and same_source:
            return "VERIFIED"
        if recovery and same_source:
            return "VERIFIED"
        return "UNVERIFIED"

    if status == "SUPERSEDED" and schema_version >= 2:
        observed_order = _source_order(proof.get("observedSourceRunId"), proof.get("observedSourceRunAttempt"))
        candidate_order = _source_order(expected_run, expected_attempt)
        valid_readback = (
            proof.get("observedStatus") == "VERIFIED"
            and _valid_sha256(proof.get("observedContentHash"))
            and _valid_sha256(proof.get("observedFilesHash"))
            and bool(proof.get("observedPublicationId"))
        )
        if valid_readback and observed_order and candidate_order and observed_order > candidate_order:
            return "SUPERSEDED"
        same_source = (
            recovery and valid_readback and observed_order == candidate_order
            and proof.get("observedSourceCommit") == expected_commit
            and proof.get("observedWindowDate") == expected_date
            and proof.get("observedWindow") == expected_window
            and proof.get("observedFilesHash") == proof.get("filesHash")
        )
        if same_source:
            return "VERIFIED"
    return "UNVERIFIED"


def _stage_evidence_for_run(run: dict[str, Any] | None, *, expected_window: str, expected_date: str) -> dict[str, Any] | None:
    if not run or not run.get("databaseId"):
        return None
    health = _download_json_artifact(run["databaseId"], "settlement-health", "settlement-health.json")
    notification = _download_json_artifact(run["databaseId"], "settlement-notification-result", "settlement-notification-result.json")
    publication = _download_json_artifact(run["databaseId"], "pages-publication-verification", "pages-publication-verification.json")
    if not health or not notification:
        return None
    if any(item.get("window") != expected_window or item.get("windowDate") != expected_date for item in (health, notification)):
        return None
    publication_status = _publication_proof_status(
        publication,
        expected_source_run_id=run.get("databaseId"),
        expected_source_run_attempt=run.get("attempt"),
        expected_commit=str(run.get("headSha") or ""),
        expected_date=expected_date,
        expected_window=expected_window,
    )
    return {
        "settlementStatus": health.get("settlementStatus"),
        "completionStatus": health.get("completionStatus"),
        "notificationStatus": notification.get("status"),
        "publicationStatus": publication_status,
        "reasonCode": notification.get("reasonCode"),
    }


def _recovery_evidence_for_source(
    source_run_id: Any,
    *,
    date_key: str,
    window: str,
    source_commit: str,
    source_run_attempt: Any | None = None,
) -> dict[str, Any] | None:
    repository = os.getenv("GITHUB_REPOSITORY", "")
    command = [
        "gh", "run", "list", "--repo", repository,
        "--workflow", "settlement-notification-recovery.yml", "--branch", "main", "--limit", "30",
        "--json", "status,conclusion,createdAt,databaseId,attempt,event,headBranch",
    ]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        runs = json.loads(completed.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    if not isinstance(runs, list):
        return None
    candidates = sorted(
        (run for run in runs if isinstance(run, dict)),
        key=lambda run: str(run.get("createdAt") or ""),
        reverse=True,
    )
    notification_proof = None
    publication_proof = None
    for run in candidates:
        if str(run.get("status") or "").lower() != "completed" or not run.get("databaseId"):
            continue
        if run.get("headBranch") not in {None, "", "main"}:
            continue
        if run.get("event") not in {None, "", "workflow_run", "workflow_dispatch"}:
            continue
        recovery_run_id = str(run["databaseId"])
        recovery_attempt = run.get("attempt")
        if notification_proof is None:
            proof = _download_json_artifact(
                recovery_run_id, "notification-recovery-result", "notification-recovery-result.json",
            )
            if (proof and str(proof.get("sourceRunId") or "") == str(source_run_id)
                    and proof.get("sourceCommit") == source_commit
                    and proof.get("windowDate") == date_key and proof.get("window") == window):
                notification_proof = proof
        if publication_proof is None:
            proof = _download_json_artifact(
                recovery_run_id, "publication-recovery-result", "pages-publication-recovery-summary.json",
            )
            status = _publication_proof_status(
                proof,
                expected_source_run_id=source_run_id,
                expected_source_run_attempt=source_run_attempt,
                expected_commit=source_commit,
                expected_date=date_key,
                expected_window=window,
                proof_run_id=recovery_run_id,
                proof_run_attempt=recovery_attempt,
                recovery=True,
            )
            if status in {"VERIFIED", "SUPERSEDED"}:
                publication_proof = {**proof, "status": status}
    if notification_proof is None and publication_proof is None:
        return None
    return {
        **(notification_proof or {}),
        "status": str((notification_proof or {}).get("status") or "UNAVAILABLE").upper(),
        "reasonCode": (notification_proof or {}).get("reasonCode"),
        "publicationStatus": str((publication_proof or {}).get("status") or "UNVERIFIED").upper(),
        "publicationProof": publication_proof,
        "sourceRunId": str(source_run_id),
        "sourceCommit": source_commit,
        "sourceRunAttempt": str(source_run_attempt) if source_run_attempt is not None else None,
        "windowDate": date_key,
        "window": window,
    }


def _write_output(result: dict[str, Any]) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"should_run={'true' if result['decision'] == 'RUN' else 'false'}\n")
            handle.write(f"window={result.get('window') or ''}\n")
            handle.write(f"snapshot_date={result.get('date') or ''}\n")
            handle.write(f"reason_code={result['reasonCode']}\n")
            handle.write(f"recovery_mode={result.get('recoveryMode') or 'settlement'}\n")
            handle.write(f"source_run_id={result.get('sourceRunId') or ''}\n")


def main() -> int:
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    schedule = os.getenv("GITHUB_EVENT_SCHEDULE", "")
    commit = os.getenv("GITHUB_SHA", "")
    api_error = None
    runs: list[dict[str, Any]] = []
    health_status = None
    stage_evidence = None
    recovery_evidence = None
    matching_run = None
    if event_name == "schedule":
        try:
            runs = _query_runs()
        except (OSError, subprocess.SubprocessError, ValueError, TypeError) as error:
            api_error = type(error).__name__
    if event_name == "schedule" and not api_error:
        context = scheduled_context(schedule)
        matching_run = _matching_primary_run(
            runs, window=str(context["window"]), snapshot_date=str(context["date"]),
        ) if context["window"] else None
        stage_evidence = _stage_evidence_for_run(
            matching_run, expected_window=str(context["window"]), expected_date=str(context["date"]),
        ) if matching_run else None
        recovery_evidence = _recovery_evidence_for_source(
            matching_run.get("databaseId"), date_key=str(context["date"]),
            window=str(context["window"]), source_commit=str(matching_run.get("headSha") or ""),
            source_run_attempt=matching_run.get("attempt"),
        ) if matching_run and matching_run.get("databaseId") else None
        health_status = _health_status_for_run(
            matching_run, expected_window=context["window"], expected_date=context["date"],
        ) if matching_run and not stage_evidence else None
    result = decide_fallback(
        event_name=event_name,
        schedule=schedule,
        commit=commit,
        runs=runs,
        api_error=api_error,
        health_status=health_status,
        stage_evidence=stage_evidence,
        recovery_evidence=recovery_evidence,
        source_run_id=str(matching_run.get("databaseId")) if matching_run and matching_run.get("databaseId") else None,
    )
    if event_name == "repository_dispatch":
        context = dispatch_context()
        result["date"] = context["date"]
        result["window"] = context["window"]
        if context["window"]:
            result["reasonCode"] = "RUN_PRIMARY_TRIGGER"
    result["apiCheck"] = "unavailable" if api_error else "ok" if event_name == "schedule" else "not_required"
    result["schemaVersion"] = 1
    Path(".private-build").mkdir(parents=True, exist_ok=True)
    with open(".private-build/schedule-gate-summary.json", "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    _write_output(result)
    print(
        "Schedule gate: "
        f"{result['decision']} window={result.get('window') or 'none'} "
        f"date={result['date']} reason={result['reasonCode']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
