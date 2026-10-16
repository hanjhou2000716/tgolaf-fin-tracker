"""Retry a confirmed, same-day settlement notification without recalculating."""

from __future__ import annotations

import datetime as dt
import argparse
import hashlib
import json
import os
from pathlib import Path

import gspread
import requests
from google.oauth2.service_account import Credentials

from dashboard_pipeline import (
    CURRENT_TRANSACTION_SPREADSHEET_ID,
    GCP_CREDENTIALS_JSON,
    LEGACY_TRANSACTION_SPREADSHEET_ID,
    mark_settlement_notification_state,
    open_spreadsheets_with_retry,
    settlement_notification_state,
)
from sheets_retry import retry_sheet_operation
from settlement_notification_state import resolve_history_sheet
from source_roles import SourceRoleConfig
from telegram_delivery import deliver_once
from telegram_delivery import decrypt_outbox
from service_contracts import TAIPEI, rfc3339_utc


def _history_sheet(*, readonly: bool = False):
    if not GCP_CREDENTIALS_JSON:
        raise RuntimeError("NOTIFICATION_CREDENTIALS_MISSING")
    credentials = Credentials.from_service_account_info(
        json.loads(GCP_CREDENTIALS_JSON),
        scopes=(
            ["https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/drive.readonly"]
            if readonly else ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        ),
    )
    client = gspread.authorize(credentials)
    ids = [value for value in (LEGACY_TRANSACTION_SPREADSHEET_ID, CURRENT_TRANSACTION_SPREADSHEET_ID) if value]
    if ids:
        workbooks = [retry_sheet_operation("notification.open_history_workbook", client.open_by_key, key) for key in dict.fromkeys(ids)]
    else:
        workbooks = open_spreadsheets_with_retry(client)
    roles = SourceRoleConfig.from_environment()
    worksheets = []
    for workbook in workbooks:
        worksheets.extend(retry_sheet_operation("notification.list_history_worksheets", workbook.worksheets))
    return resolve_history_sheet(worksheets, roles.role_for)


def recover(envelope: dict, *, token: str, chat_id: str, history_sheet, expected_run_id: str,
            expected_commit: str, source_result: dict | None = None,
            source_health: dict | None = None, publication_verification: dict | None = None,
            post=requests.post, now=None, send: bool = True):
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(TAIPEI).date().isoformat()
    date_key = envelope.get("windowDate")
    window = envelope.get("window")
    payload = envelope.get("payload")
    if envelope.get("schemaVersion") != 1 or envelope.get("notificationType") != "settlement":
        return {"status": "FAILED", "reasonCode": "OUTBOX_SCHEMA_UNSUPPORTED"}
    supplied_hash = envelope.get("contentHash")
    unsigned = dict(envelope)
    unsigned.pop("contentHash", None)
    expected_hash = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if supplied_hash != expected_hash:
        return {"status": "FAILED", "reasonCode": "OUTBOX_HASH_INVALID"}
    if str(envelope.get("runId")) != str(expected_run_id) or envelope.get("sourceCommit") != expected_commit:
        return {"status": "FAILED", "reasonCode": "OUTBOX_RUN_IDENTITY_MISMATCH"}
    if date_key != today or window not in {"us", "tw"}:
        return {"status": "EXPIRED", "reasonCode": "OUTBOX_NOT_CURRENT_SETTLEMENT"}
    if not isinstance(payload, dict) or str(payload.get("chat_id")) != str(chat_id):
        return {"status": "FAILED", "reasonCode": "OUTBOX_PAYLOAD_INVALID"}
    if source_result is not None:
        if (source_result.get("notificationType") not in {None, "settlement"}
                or source_result.get("windowDate") != date_key
                or source_result.get("window") != window):
            return {"status": "FAILED", "reasonCode": "SOURCE_NOTIFICATION_IDENTITY_MISMATCH",
                    "failureStage": "SOURCE_RESULT_VALIDATION", "sourceRunId": str(expected_run_id)}
        if source_result.get("status") in {"SENT", "SUPPRESSED"}:
            return {"status": "SENT", "reasonCode": "SOURCE_RECEIPT_ALREADY_CONFIRMED",
                    "priorReceipt": source_result.get("priorReceipt"), "sourceRunId": str(expected_run_id),
                    "sourceCommit": expected_commit}
        if source_result.get("status") == "DISABLED":
            return {"status": "BLOCKED_UPSTREAM", "completionStatus": "INCOMPLETE",
                    "reasonCode": "SOURCE_NOTIFICATION_DISABLED", "sourceRunId": str(expected_run_id)}
        if source_result.get("status") not in {"FAILED", "UNCONFIRMED", "PENDING"}:
            return {"status": "BLOCKED_UPSTREAM", "completionStatus": "INCOMPLETE",
                    "reasonCode": "SOURCE_NOTIFICATION_NOT_RECOVERABLE", "sourceRunId": str(expected_run_id)}
        source_reason = source_result.get("reasonCode")
        if source_reason not in {"NOTIFICATION_STATE_UNAVAILABLE", "NOTIFICATION_STATE_WRITE_FAILED", "TELEGRAM_RATE_LIMITED"}:
            return {"status": "BLOCKED_UPSTREAM", "completionStatus": "INCOMPLETE",
                    "reasonCode": "SOURCE_FAILURE_NOT_AUTOMATICALLY_RETRYABLE", "sourceRunId": str(expected_run_id)}
        if source_result.get("httpAttempted") is True and source_reason != "TELEGRAM_RATE_LIMITED":
            return {"status": "BLOCKED_UPSTREAM", "completionStatus": "INCOMPLETE",
                    "reasonCode": "SOURCE_DELIVERY_MAY_BE_AMBIGUOUS", "sourceRunId": str(expected_run_id)}
    if source_health is not None:
        if source_health.get("windowDate") != date_key or source_health.get("window") != window:
            return {"status": "FAILED", "reasonCode": "SOURCE_SETTLEMENT_IDENTITY_MISMATCH",
                    "failureStage": "SOURCE_HEALTH_VALIDATION", "sourceRunId": str(expected_run_id)}
        if source_health.get("settlementStatus") != "COMPLETE":
            return {"status": "BLOCKED_UPSTREAM", "completionStatus": "INCOMPLETE",
                    "reasonCode": "SOURCE_SETTLEMENT_NOT_COMPLETE", "sourceRunId": str(expected_run_id),
                    "sourceCommit": expected_commit}

    try:
        prior = settlement_notification_state(history_sheet, date_key, window)
    except Exception as error:
        source_reason = source_result.get("reasonCode") if isinstance(source_result, dict) else None
        if source_reason == "NOTIFICATION_STATE_UNAVAILABLE":
            return {
                "schemaVersion": 2, "status": "BLOCKED_UPSTREAM", "completionStatus": "INCOMPLETE",
                "reasonCode": "UPSTREAM_NOTIFICATION_STATE_STILL_UNAVAILABLE",
                "upstreamReasonCode": getattr(error, "reason_code", None) or "NOTIFICATION_STATE_UNAVAILABLE",
                "failureStage": "RECOVERY_PREFLIGHT", "httpAttempted": False,
                "sourceRunId": str(expected_run_id), "sourceCommit": expected_commit,
                "windowDate": date_key, "window": window,
            }
        return {
            "schemaVersion": 2, "status": "FAILED", "reasonCode": getattr(error, "reason_code", None) or "NOTIFICATION_STATE_UNAVAILABLE",
            "failureStage": "RECOVERY_PREFLIGHT", "httpAttempted": False,
            "sourceRunId": str(expected_run_id), "sourceCommit": expected_commit,
            "windowDate": date_key, "window": window,
        }
    if prior.get("status") == "SENT":
        return {"schemaVersion": 2, "status": "SENT", "reasonCode": "ALREADY_SENT",
                "priorReceipt": {key: prior.get(key) for key in ("sentAt", "messageId", "attemptId", "sourceRunId", "sourceCommit") if prior.get(key) is not None},
                "sourceRunId": str(expected_run_id), "sourceCommit": expected_commit,
                "windowDate": date_key, "window": window}
    if prior.get("status") in {"SENDING", "DELIVERY_UNKNOWN"}:
        return {"schemaVersion": 2, "status": "DELIVERY_UNKNOWN", "reasonCode": "PRIOR_ATTEMPT_UNCONFIRMED",
                "failureStage": "RECOVERY_PREFLIGHT", "httpAttempted": False,
                "sourceRunId": str(expected_run_id), "sourceCommit": expected_commit,
                "windowDate": date_key, "window": window}
    if not send:
        return {"schemaVersion": 2, "status": "READY_TO_SEND", "reasonCode": "DRY_RUN_NO_SIDE_EFFECT",
                "httpAttempted": False, "sourceRunId": str(expected_run_id), "sourceCommit": expected_commit,
                "windowDate": date_key, "window": window}
    result = deliver_once(
        date_key=date_key,
        window=window,
        payload=payload,
        token=token,
        load_state=lambda: settlement_notification_state(history_sheet, date_key, window),
        save_state=lambda state: mark_settlement_notification_state(history_sheet, date_key, window, state),
        post=post,
        now=rfc3339_utc(now),
        source_metadata={
            "sourceRunId": str(expected_run_id), "sourceCommit": expected_commit,
            "notificationType": "settlement", "messageKind": envelope.get("messageKind"),
        },
    )
    result.setdefault("sourceRunId", str(expected_run_id))
    result.setdefault("sourceCommit", expected_commit)
    result["publicationStatus"] = (
        "VERIFIED" if isinstance(publication_verification, dict)
        and publication_verification.get("status") == "VERIFIED" else "UNVERIFIED"
    )
    return result


def _read_json(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("SOURCE_EVIDENCE_INVALID")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Preflight or recover one authenticated settlement notification.")
    parser.add_argument("--mode", choices=("plan", "send"), default=os.getenv("RECOVERY_MODE", "plan"))
    parser.add_argument("--source-run-id", default=os.getenv("RECOVERY_SOURCE_RUN_ID", ""))
    parser.add_argument("--date", default=os.getenv("RECOVERY_WINDOW_DATE", ""))
    parser.add_argument("--window", choices=("us", "tw"), default=os.getenv("RECOVERY_WINDOW", ""))
    args = parser.parse_args(argv)
    result = {"schemaVersion": 2, "status": "FAILED", "reasonCode": "OUTBOX_UNAVAILABLE"}
    try:
        if not (args.source_run_id and args.date and args.window):
            raise ValueError("SEND_SCOPE_REQUIRED")
        encrypted = json.loads(Path(".private-build/settlement-notification-outbox.enc.json").read_text(encoding="utf-8"))
        source_result = _read_json(os.getenv(
            "SOURCE_NOTIFICATION_RESULT_PATH",
            ".private-build/source-notification/settlement-notification-result.json",
        ))
        source_health = _read_json(os.getenv(
            "SOURCE_SETTLEMENT_HEALTH_PATH",
            ".private-build/source-health/settlement-health.json",
        ))
        publication_path = os.getenv(
            "SOURCE_PUBLICATION_VERIFICATION_PATH",
            ".private-build/source-publication/pages-publication-verification.json",
        )
        publication_verification = _read_json(publication_path) if Path(publication_path).exists() else None
        envelope = decrypt_outbox(
            encrypted,
            telegram_token=os.getenv("TELEGRAM_TOKEN", ""),
            google_credentials=GCP_CREDENTIALS_JSON or "",
        )
        if envelope.get("windowDate") != args.date or envelope.get("window") != args.window:
            result = {"schemaVersion": 2, "status": "FAILED", "reasonCode": "REQUESTED_WINDOW_MISMATCH",
                      "failureStage": "RECOVERY_SCOPE_VALIDATION", "httpAttempted": False}
        else:
            result = recover(
                envelope,
                token=os.getenv("TELEGRAM_TOKEN", ""),
                chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
                history_sheet=_history_sheet(readonly=args.mode != "send"),
                expected_run_id=args.source_run_id,
                expected_commit=os.getenv("RECOVERY_SOURCE_COMMIT", ""),
                source_result=source_result,
                source_health=source_health,
                publication_verification=publication_verification,
                send=args.mode == "send",
            )
        result.setdefault("sourceRunId", args.source_run_id)
        result.setdefault("sourceCommit", os.getenv("RECOVERY_SOURCE_COMMIT", ""))
    except Exception as error:
        result = {
            "schemaVersion": 2, "status": "FAILED", "failureStage": "RECOVERY_EXECUTION",
            "reasonCode": getattr(error, "reason_code", None) or type(error).__name__,
            "httpAttempted": False,
            "sourceRunId": os.getenv("RECOVERY_SOURCE_RUN_ID", ""),
            "sourceCommit": os.getenv("RECOVERY_SOURCE_COMMIT", ""),
        }
    Path(".private-build").mkdir(parents=True, exist_ok=True)
    Path(".private-build/notification-recovery-result.json").write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in result.items() if key != "messageId"}, ensure_ascii=False))
    return 1 if result.get("status") in {"FAILED", "DELIVERY_UNKNOWN"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
