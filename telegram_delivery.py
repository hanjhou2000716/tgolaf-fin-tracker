"""Small, testable Telegram delivery boundary for settlement messages."""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Callable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


_OUTBOX_AAD_PREFIX = b"PRStK-settlement-outbox-v1\n"


def _outbox_key(telegram_token: str, google_credentials: str) -> bytes:
    if not telegram_token or not google_credentials:
        raise ValueError("outbox encryption credentials are unavailable")
    material = (telegram_token + "\n" + google_credentials).encode("utf-8")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"growth-settlement-notification-outbox-v1",
        info=b"AES-256-GCM key derivation",
    ).derive(material)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encrypt_outbox(envelope: dict[str, Any], *, telegram_token: str, google_credentials: str) -> dict[str, Any]:
    metadata = {key: envelope.get(key) for key in (
        "schemaVersion", "notificationType", "windowDate", "window", "sourceCommit", "runId"
    )}
    nonce = os.urandom(12)
    ciphertext = AESGCM(_outbox_key(telegram_token, google_credentials)).encrypt(
        nonce, _canonical_json(envelope), _OUTBOX_AAD_PREFIX + _canonical_json(metadata)
    )
    return {
        **metadata,
        "encryption": "AES-256-GCM/HKDF-SHA256",
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def decrypt_outbox(value: dict[str, Any], *, telegram_token: str, google_credentials: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("encryption") != "AES-256-GCM/HKDF-SHA256":
        raise ValueError("outbox encryption format is unsupported")
    metadata = {key: value.get(key) for key in (
        "schemaVersion", "notificationType", "windowDate", "window", "sourceCommit", "runId"
    )}
    try:
        plaintext = AESGCM(_outbox_key(telegram_token, google_credentials)).decrypt(
            base64.b64decode(value["nonce"], validate=True),
            base64.b64decode(value["ciphertext"], validate=True),
            _OUTBOX_AAD_PREFIX + _canonical_json(metadata),
        )
        envelope = json.loads(plaintext)
    except Exception as error:
        raise ValueError("outbox authentication failed") from error
    if not isinstance(envelope, dict) or any(envelope.get(key) != item for key, item in metadata.items()):
        raise ValueError("outbox identity does not match authenticated metadata")
    return envelope

class TelegramDeliveryError(RuntimeError):
    def __init__(self, status: str, reason_code: str):
        super().__init__(reason_code)
        self.status = status
        self.reason_code = reason_code


def send_message(
    token: str,
    payload: dict[str, Any],
    *,
    post: Callable[..., Any],
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 3,
) -> dict[str, Any]:
    """Send only when Telegram can return a verifiable accepted Message.

    Explicit 429 responses can be retried. Transport failures and server
    errors are ambiguous, so they are returned as DELIVERY_UNKNOWN and are
    never retried automatically.
    """
    if not token:
        raise TelegramDeliveryError("FAILED", "TELEGRAM_CREDENTIALS_MISSING")
    for attempt in range(max(1, min(int(attempts), 3))):
        try:
            response = post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
                timeout=10,
            )
        except Exception as error:
            # requests exceptions do not reliably prove whether the request
            # reached Telegram. Never retry an ambiguous delivery.
            response = getattr(error, "response", None)
            if response is None:
                raise TelegramDeliveryError("DELIVERY_UNKNOWN", "TELEGRAM_TRANSPORT_UNKNOWN") from error
            status_code = getattr(response, "status_code", None)
            if not isinstance(status_code, int):
                raise TelegramDeliveryError("DELIVERY_UNKNOWN", "TELEGRAM_RESPONSE_UNKNOWN") from error
            if status_code >= 500:
                raise TelegramDeliveryError("DELIVERY_UNKNOWN", "TELEGRAM_SERVER_UNKNOWN") from error
            if status_code != 429:
                raise TelegramDeliveryError("FAILED", f"TELEGRAM_HTTP_{status_code}") from error
        status_code = getattr(response, "status_code", None)
        try:
            body = response.json()
        except Exception as error:
            raise TelegramDeliveryError("DELIVERY_UNKNOWN", "TELEGRAM_INVALID_RESPONSE") from error
        if status_code == 429:
            parameters = body.get("parameters") if isinstance(body, dict) else None
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            if attempt + 1 >= attempts or not isinstance(retry_after, (int, float)) or retry_after < 0:
                raise TelegramDeliveryError("FAILED", "TELEGRAM_RATE_LIMITED")
            sleep(float(retry_after))
            continue
        if isinstance(status_code, int) and status_code >= 500:
            raise TelegramDeliveryError("DELIVERY_UNKNOWN", "TELEGRAM_SERVER_UNKNOWN")
        if isinstance(status_code, int) and status_code >= 400:
            raise TelegramDeliveryError("FAILED", f"TELEGRAM_HTTP_{status_code}")
        if not isinstance(status_code, int) or status_code < 200:
            raise TelegramDeliveryError("DELIVERY_UNKNOWN", "TELEGRAM_HTTP_STATUS_UNKNOWN")
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise TelegramDeliveryError("FAILED", "TELEGRAM_REJECTED")
        result = body.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, (int, str)) or not str(message_id).isdigit():
            raise TelegramDeliveryError("DELIVERY_UNKNOWN", "TELEGRAM_MESSAGE_ID_MISSING")
        return {"status": "SENT", "messageId": str(message_id)}
    raise TelegramDeliveryError("FAILED", "TELEGRAM_RATE_LIMITED")


def deliver_once(
    *,
    date_key: str,
    window: str,
    payload: dict[str, Any],
    token: str,
    load_state: Callable[[], dict[str, Any]],
    save_state: Callable[[dict[str, Any]], None],
    post: Callable[..., Any],
    now: str,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Apply a write-ahead marker so interrupted sends are never replayed."""
    try:
        prior = load_state()
    except Exception:
        return {"schemaVersion": 1, "windowDate": date_key, "window": window,
                "status": "FAILED", "reasonCode": "NOTIFICATION_STATE_UNAVAILABLE"}
    prior_status = str(prior.get("status", "PENDING"))
    if prior_status == "SENT":
        return {"schemaVersion": 1, "windowDate": date_key, "window": window,
                "status": "SENT", "reasonCode": "ALREADY_SENT"}
    if prior_status in {"SENDING", "DELIVERY_UNKNOWN"}:
        return {"schemaVersion": 1, "windowDate": date_key, "window": window,
                "status": "DELIVERY_UNKNOWN", "reasonCode": "PRIOR_ATTEMPT_UNCONFIRMED"}
    if prior_status == "FAILED" and prior.get("reasonCode") != "TELEGRAM_RATE_LIMITED":
        return {"schemaVersion": 1, "windowDate": date_key, "window": window,
                "status": "FAILED", "reasonCode": str(prior.get("reasonCode") or "TELEGRAM_REJECTED")}
    sending = {"status": "SENDING", "attemptedAt": now}
    try:
        save_state(sending)
    except Exception:
        return {"schemaVersion": 1, "windowDate": date_key, "window": window,
                "status": "FAILED", "reasonCode": "NOTIFICATION_STATE_WRITE_FAILED"}
    try:
        receipt = send_message(token, payload, post=post, sleep=sleep)
    except TelegramDeliveryError as error:
        result = {"schemaVersion": 1, "windowDate": date_key, "window": window,
                  "status": error.status, "reasonCode": error.reason_code}
        try:
            save_state({**result, "attemptedAt": now})
        except Exception:
            # The previously committed SENDING state still prevents a resend.
            result = {**result, "status": "DELIVERY_UNKNOWN",
                      "reasonCode": "NOTIFICATION_STATE_WRITE_FAILED"}
        return result
    sent = {"status": "SENT", "sentAt": now, "messageId": receipt["messageId"]}
    try:
        save_state(sent)
    except Exception:
        return {"schemaVersion": 1, "windowDate": date_key, "window": window,
                "status": "DELIVERY_UNKNOWN", "reasonCode": "SENT_STATE_WRITE_FAILED"}
    return {"schemaVersion": 1, "windowDate": date_key, "window": window,
            "status": "SENT", "sentAt": now, "messageId": receipt["messageId"]}
