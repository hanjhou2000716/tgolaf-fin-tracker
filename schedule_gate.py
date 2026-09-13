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


def decide_fallback(
    *,
    event_name: str,
    schedule: str,
    now_utc: dt.datetime | None = None,
    commit: str = "",
    runs: Iterable[dict[str, Any]] | None = None,
    api_error: str | None = None,
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
    }
    if event_name != "schedule":
        return result
    if not window:
        result["reasonCode"] = "RUN_UNKNOWN_SCHEDULE"
        return result
    if api_error:
        result["reasonCode"] = "RUN_API_UNAVAILABLE"
        return result
    if _successful_run_matches(runs or (), window=window, snapshot_date=str(context["date"]), commit=commit):
        result["foundSuccessfulRun"] = True
        result["decision"] = "SKIP"
        result["reasonCode"] = "SKIP_ALREADY_SUCCEEDED"
    else:
        result["reasonCode"] = "RUN_NO_SUCCESSFUL_MATCH"
    return result


def _query_runs() -> list[dict[str, Any]]:
    repository = os.getenv("GITHUB_REPOSITORY", "")
    workflow = os.getenv("GITHUB_WORKFLOW_REF", "").split("@", 1)[0].rsplit("/", 1)[-1] or "cron.yml"
    command = [
        "gh", "run", "list", "--repo", repository, "--workflow", workflow,
        "--branch", "main", "--limit", "50", "--json",
        "status,conclusion,headSha,createdAt,event,databaseId",
    ]
    completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    value = json.loads(completed.stdout or "[]")
    return value if isinstance(value, list) else []


def _write_output(result: dict[str, Any]) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"should_run={'true' if result['decision'] == 'RUN' else 'false'}\n")
            handle.write(f"window={result.get('window') or ''}\n")
            handle.write(f"snapshot_date={result.get('date') or ''}\n")
            handle.write(f"reason_code={result['reasonCode']}\n")


def main() -> int:
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    schedule = os.getenv("GITHUB_EVENT_SCHEDULE", "")
    commit = os.getenv("GITHUB_SHA", "")
    api_error = None
    runs: list[dict[str, Any]] = []
    if event_name == "schedule":
        try:
            runs = _query_runs()
        except (OSError, subprocess.SubprocessError, ValueError, TypeError) as error:
            api_error = type(error).__name__
    result = decide_fallback(
        event_name=event_name,
        schedule=schedule,
        commit=commit,
        runs=runs,
        api_error=api_error,
    )
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
