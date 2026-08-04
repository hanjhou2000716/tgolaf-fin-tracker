"""Deterministic alert policy engine with cooldown and recovery state."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import IntEnum


class Severity(IntEnum):
    INFO = 1
    WATCH = 2
    WARNING = 3
    CRITICAL = 4


@dataclass
class AlertState:
    last_sent_at: str | None = None
    active: bool = False
    acknowledged: bool = False


class AlertEngine:
    def __init__(self, *, cooldown_minutes=180, now=None):
        self.cooldown = timedelta(minutes=cooldown_minutes)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.states = {}

    def evaluate(self, metrics):
        rules = [
            ("maintenance_warning", Severity.WARNING, metrics.get("maintenanceRatio", 0) < 180, "維持率低於 180%"),
            ("maintenance_critical", Severity.CRITICAL, metrics.get("stressMaintenanceRatio", 999) < 150, "壓力後維持率低於 150%"),
            ("concentration_warning", Severity.WARNING, metrics.get("maxCompanyExposure", 0) > 35, "單一公司曝險超過 35%"),
            ("cash_watch", Severity.WATCH, metrics.get("cashMonths", 999) < 6, "現金低於六個月需求"),
            ("stale_warning", Severity.WARNING, metrics.get("isStale", False), "行情超過 24 小時未更新"),
            ("reconciliation_critical", Severity.CRITICAL, not metrics.get("reconciled", True), "帳本與快照無法對帳"),
        ]
        now = self.now()
        results = []
        for key, severity, triggered, message in rules:
            state = self.states.setdefault(key, AlertState())
            if triggered:
                recovered = False
                should_send = not state.active or not state.last_sent_at or now - datetime.fromisoformat(state.last_sent_at) >= self.cooldown
                if should_send:
                    state.last_sent_at = now.isoformat()
                state.active = True
                state.acknowledged = False if should_send else state.acknowledged
            else:
                recovered = state.active
                state.active = False
                state.acknowledged = False
                should_send = recovered
            results.append({
                "key": key,
                "severity": severity.name,
                "triggered": bool(triggered),
                "send": bool(should_send),
                "recovered": bool(recovered),
                "acknowledged": state.acknowledged,
                "message": message,
            })
        return results

    def acknowledge(self, key):
        if key in self.states and self.states[key].active:
            self.states[key].acknowledged = True
            return True
        return False
