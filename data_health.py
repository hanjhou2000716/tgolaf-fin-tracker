"""Data Health page contract for freshness, source quality and reconciliation."""

from collections.abc import Mapping
from datetime import datetime, timezone


def build_data_health(*, last_sync, sources, missing=None, pending_transactions=0, reconciled=True, stale_after_hours=18, now=None) -> dict:
    clock = now or datetime.now(timezone.utc)
    synced = datetime.fromisoformat(str(last_sync).replace("Z", "+00:00"))
    age_hours = max(0.0, (clock - synced).total_seconds() / 3600)
    source_rows = []
    if isinstance(sources, Mapping):
        source_items = sources.items()
    else:
        source_items = ((row.get("name"), row) for row in (sources or []) if isinstance(row, Mapping) and row.get("name"))
    for name, source in source_items:
        source = dict(source or {})
        source_rows.append({"name": name, "quality": source.get("quality", "unknown"), "source": source.get("source", name), "fallbackUsed": bool(source.get("fallback_used", source.get("fallbackUsed", False))), "asOf": source.get("as_of", source.get("asOf"))})
    return {
        "lastSync": str(last_sync),
        "ageHours": round(age_hours, 2),
        "stale": age_hours > float(stale_after_hours),
        "sources": source_rows,
        "missing": list(missing or []),
        "pendingTransactions": int(pending_transactions),
        "reconciled": bool(reconciled),
        "status": "critical" if not reconciled or missing else "stale" if age_hours > stale_after_hours else "healthy",
    }
