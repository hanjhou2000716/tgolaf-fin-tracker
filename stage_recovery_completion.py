"""Validate and summarize fallback runs that recover only missing stages."""

from __future__ import annotations

import json
import os
from pathlib import Path


NOTIFICATION_TERMINAL = {"SENT", "SUPPRESSED", "ALREADY_SENT", "SOURCE_RECEIPT_ALREADY_CONFIRMED"}
NOTIFICATION_EVALUATED = {"BLOCKED_UPSTREAM", "EXPIRED"}
PUBLICATION_TERMINAL = {"VERIFIED", "SUPERSEDED"}


def build_summary(mode: str, notification_status: str = "", publication_status: str = "") -> dict:
    needs_notification = mode in {"notification_only", "notification_and_publication"}
    needs_publication = mode in {"publication_only", "notification_and_publication"}
    errors = []
    incomplete = False
    if needs_notification:
        if notification_status in NOTIFICATION_EVALUATED:
            incomplete = True
        elif notification_status not in NOTIFICATION_TERMINAL:
            errors.append("NOTIFICATION_RECOVERY_NOT_CONFIRMED")
    if needs_publication:
        if publication_status not in PUBLICATION_TERMINAL:
            errors.append("PUBLICATION_RECOVERY_NOT_CONFIRMED")
    return {
        "schemaVersion": 1,
        "recoveryMode": mode,
        "notificationStatus": notification_status or None,
        "publicationStatus": publication_status or None,
        "completionStatus": "INCOMPLETE" if incomplete or errors else "COMPLETE",
        "reasonCodes": errors or (["UPSTREAM_RECOVERY_BLOCKED"] if incomplete else []),
    }


def main() -> int:
    summary = build_summary(
        os.getenv("RECOVERY_MODE", ""),
        os.getenv("RECOVERY_NOTIFICATION_STATUS", ""),
        os.getenv("RECOVERY_PUBLICATION_STATUS", ""),
    )
    path = Path(".private-build/stage-recovery-completion.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 1 if summary["reasonCodes"] and summary["reasonCodes"] != ["UPSTREAM_RECOVERY_BLOCKED"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
