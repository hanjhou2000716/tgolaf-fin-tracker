"""Retry a confirmed, same-day settlement notification without recalculating."""

from __future__ import annotations

import datetime as dt
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
from source_roles import SourceRoleConfig
from telegram_delivery import deliver_once
from telegram_delivery import decrypt_outbox
from service_contracts import TAIPEI, rfc3339_utc


def _history_sheet():
    if not GCP_CREDENTIALS_JSON:
        raise RuntimeError("NOTIFICATION_CREDENTIALS_MISSING")
    credentials = Credentials.from_service_account_info(
        json.loads(GCP_CREDENTIALS_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"],
    )
    client = gspread.authorize(credentials)
    ids = [value for value in (LEGACY_TRANSACTION_SPREADSHEET_ID, CURRENT_TRANSACTION_SPREADSHEET_ID) if value]
    if ids:
        workbooks = [retry_sheet_operation("notification.open_history_workbook", client.open_by_key, key) for key in dict.fromkeys(ids)]
    else:
        workbooks = open_spreadsheets_with_retry(client)
    roles = SourceRoleConfig.from_environment()
    for workbook in workbooks:
        for worksheet in retry_sheet_operation("notification.list_history_worksheets", workbook.worksheets):
            if roles.role_for(worksheet.title) == "HISTORY":
                return worksheet
    raise RuntimeError("NOTIFICATION_HISTORY_UNAVAILABLE")


def recover(envelope: dict, *, token: str, chat_id: str, history_sheet, expected_run_id: str,
            expected_commit: str, post=requests.post, now=None):
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
    return deliver_once(
        date_key=date_key,
        window=window,
        payload=payload,
        token=token,
        load_state=lambda: settlement_notification_state(history_sheet, date_key, window),
        save_state=lambda state: mark_settlement_notification_state(history_sheet, date_key, window, state),
        post=post,
        now=rfc3339_utc(now),
    )


def main():
    result = {"schemaVersion": 1, "status": "FAILED", "reasonCode": "OUTBOX_UNAVAILABLE"}
    try:
        encrypted = json.loads(Path(".private-build/settlement-notification-outbox.enc.json").read_text(encoding="utf-8"))
        envelope = decrypt_outbox(
            encrypted,
            telegram_token=os.getenv("TELEGRAM_TOKEN", ""),
            google_credentials=GCP_CREDENTIALS_JSON or "",
        )
        result = recover(
            envelope,
            token=os.getenv("TELEGRAM_TOKEN", ""),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            history_sheet=_history_sheet(),
            expected_run_id=os.getenv("RECOVERY_SOURCE_RUN_ID", ""),
            expected_commit=os.getenv("RECOVERY_SOURCE_COMMIT", ""),
        )
    except Exception as error:
        result = {"schemaVersion": 1, "status": "FAILED", "reasonCode": type(error).__name__}
    Path(".private-build").mkdir(parents=True, exist_ok=True)
    Path(".private-build/notification-recovery-result.json").write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in result.items() if key != "messageId"}, ensure_ascii=False))
    return 1 if result.get("status") in {"FAILED", "DELIVERY_UNKNOWN"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
