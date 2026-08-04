"""Named alert policy and adapter for dashboard/Telegram consumers."""

from datetime import datetime, timezone

from alerts import AlertEngine


POLICY = {
    "maintenance_warning": {"severity": "WARNING", "threshold": "maintenanceRatio < 180%"},
    "maintenance_critical": {"severity": "CRITICAL", "threshold": "stressMaintenanceRatio < 150%"},
    "concentration_warning": {"severity": "WARNING", "threshold": "maxCompanyExposure > 35%"},
    "cash_watch": {"severity": "WATCH", "threshold": "cashMonths < 6"},
    "stale_warning": {"severity": "WARNING", "threshold": "quote age > 24h"},
    "reconciliation_critical": {"severity": "CRITICAL", "threshold": "ledger snapshot mismatch"},
}


def evaluate_alert_policy(metrics: dict, *, engine: AlertEngine | None = None, now=None) -> dict:
    """Evaluate the canonical policy and return active, recovery and sends."""
    normalized = dict(metrics)
    if "stressMaintenanceRatio" not in normalized and "stressRatio" in normalized:
        normalized["stressMaintenanceRatio"] = normalized["stressRatio"]
    if "quoteFetchedAt" in normalized and "isStale" not in normalized:
        fetched = datetime.fromisoformat(str(normalized["quoteFetchedAt"]).replace("Z", "+00:00"))
        clock = now or datetime.now(timezone.utc)
        normalized["isStale"] = (clock - fetched).total_seconds() > 24 * 3600
    result = (engine or AlertEngine(now=(lambda: now) if now else None)).evaluate(normalized)
    return {"policy": POLICY, "alerts": result, "active": [item for item in result if item["triggered"]], "recovered": [item for item in result if item["recovered"]]}
