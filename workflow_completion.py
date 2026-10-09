"""Fail the workflow only after independent settlement and Pages stages run."""

from __future__ import annotations

import json
import os
from pathlib import Path


def completion_errors(env=None, notification=None, settlement_health=None):
    env = env or os.environ
    errors = []
    for name in ("BUILD_RESULT", "DEPLOY_RESULT", "PUBLICATION_RESULT"):
        if str(env.get(name, "")).lower() != "success":
            errors.append(name.removesuffix("_RESULT") + "_NOT_CONFIRMED")
    if str(env.get("REQUIRE_NOTIFICATION", "false")).lower() in {"true", "1", "yes"}:
        if not isinstance(settlement_health, dict):
            errors.append("SETTLEMENT_RESULT_MISSING")
        elif settlement_health.get("completionStatus") != "COMPLETE":
            completion_reasons = settlement_health.get("completionReasonCodes", [])
            errors.append("SETTLEMENT_STAGES_NOT_COMPLETE")
            errors.extend(completion_reasons or ["SETTLEMENT_COMPLETION_UNVERIFIED"])
        if not isinstance(notification, dict):
            errors.append("NOTIFICATION_RESULT_MISSING")
        elif (
            notification.get("status") != "SENT"
            or notification.get("notificationType") != "settlement"
            or notification.get("windowDate") != env.get("EXPECTED_DATE")
            or notification.get("window") != env.get("EXPECTED_WINDOW")
        ):
            errors.append(str(notification.get("reasonCode") or "NOTIFICATION_NOT_CONFIRMED"))
    return errors


def main():
    notification = None
    settlement_health = None
    try:
        notification = json.loads(
            Path(".private-build/settlement-notification-result.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        pass
    try:
        settlement_health = json.loads(
            Path(".private-build/settlement-health.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        pass
    errors = completion_errors(notification=notification, settlement_health=settlement_health)
    if errors:
        print("Settlement completion failed: " + ", ".join(errors))
        return 1
    print("Settlement completion verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
