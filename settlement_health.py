"""Create a non-financial private health marker for conditional fallback gates."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping


def _read(path: str | Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def build_settlement_health(
    status_payload: Mapping[str, Any] | None,
    private_payload: Mapping[str, Any] | None,
    notification_payload: Mapping[str, Any] | None = None,
    *,
    window_date: str = "",
    window: str = "",
) -> dict[str, Any]:
    reasons: list[str] = []
    if not isinstance(status_payload, Mapping) or status_payload.get("status") != "ok":
        reasons.append("PORTFOLIO_STATUS_NOT_OK")
    if window in {"us", "tw"}:
        if not isinstance(notification_payload, Mapping):
            reasons.append("SETTLEMENT_NOTIFICATION_UNVERIFIED")
        elif (
            notification_payload.get("status") != "SENT"
            or notification_payload.get("notificationType") != "settlement"
            or notification_payload.get("windowDate") != window_date
            or notification_payload.get("window") != window
        ):
            reasons.append("SETTLEMENT_NOTIFICATION_NOT_CONFIRMED")
    if not isinstance(private_payload, Mapping):
        reasons.append("PRIVATE_SNAPSHOT_MISSING")
    else:
        portfolio = private_payload.get("portfolio")
        risk = portfolio.get("risk") if isinstance(portfolio, Mapping) else None
        beta = risk.get("beta") if isinstance(risk, Mapping) else None
        kelly = risk.get("kelly") if isinstance(risk, Mapping) else None
        if not isinstance(beta, Mapping) or beta.get("status") != "READY" or beta.get("policyStatus") != "READY":
            reasons.append("NAV_BETA_NOT_READY")
        if not isinstance(beta, Mapping) or beta.get("validationStatus") != "AUTO_VALIDATED":
            reasons.append("BETA_POLICY_NOT_AUTO_VALIDATED")
        if not isinstance(beta, Mapping) or beta.get("marketQuotesFresh") is not True:
            reasons.append("MARKET_QUOTES_NOT_FRESH")
        if not isinstance(kelly, Mapping) or kelly.get("status") != "READY":
            reasons.append("KELLY_NOT_READY")
        if not isinstance(kelly, Mapping) or kelly.get("approvalStatus") != "AUTO_VALIDATED":
            reasons.append("KELLY_POLICY_NOT_AUTO_VALIDATED")
        ledger_audit = status_payload.get("ledgerAudit") if isinstance(status_payload, Mapping) else None
        if not isinstance(ledger_audit, Mapping) or ledger_audit.get("status") != "OK":
            reasons.append("LEDGER_AUDIT_NOT_OK")
        ingestion = status_payload.get("ingestionHealth") if isinstance(status_payload, Mapping) else None
        if not isinstance(ingestion, Mapping) or str(ingestion.get("status", "")).upper() not in {"OK", "READY", "READY_FROM_FORM"}:
            reasons.append("INGESTION_NOT_READY")
    result = {
        "schemaVersion": 1,
        "healthStatus": "PASS" if not reasons else "UNHEALTHY",
        "windowDate": window_date or None,
        "window": window or None,
        "reasonCodes": sorted(set(reasons)),
        "publicationVerificationRequired": True,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", default=".private-build/status.private.json")
    parser.add_argument("--snapshot", default=".private-build/data.private.json")
    parser.add_argument("--notification", default=".private-build/settlement-notification-result.json")
    parser.add_argument("--output", default=".private-build/settlement-health.json")
    args = parser.parse_args()
    result = build_settlement_health(
        _read(args.status), _read(args.snapshot), _read(args.notification),
        window_date=os.getenv("SCHEDULED_DATE_OVERRIDE", ""),
        window=os.getenv("SCHEDULED_WINDOW_OVERRIDE", ""),
    )
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
