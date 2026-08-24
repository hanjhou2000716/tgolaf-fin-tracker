"""Pure safety checks for rebuilding a portfolio from Google Sheet rows.

This module deliberately contains no network or spreadsheet code.  Keeping the
decision function pure makes the zero-result guard testable without exposing a
private snapshot or requiring production credentials.
"""

from __future__ import annotations

import math
from typing import Any


ASSET_BUCKETS = ("台股", "美股", "基金", "現金_TWD", "現金_USD", "擔保品")


def inventory_has_positive_assets(inventory: dict[str, Any] | None) -> bool:
    """Return True only when a real asset bucket contains a positive value."""
    if not isinstance(inventory, dict):
        return False
    for bucket in ASSET_BUCKETS:
        values = inventory.get(bucket, {})
        candidates = values.values() if isinstance(values, dict) else (values,)
        for value in candidates:
            try:
                if math.isfinite(float(value)) and float(value) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _has_invalid_asset_number(inventory: dict[str, Any]) -> bool:
    for bucket in ASSET_BUCKETS:
        values = inventory.get(bucket, {})
        candidates = values.values() if isinstance(values, dict) else (values,)
        for value in candidates:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return True
            if not math.isfinite(number) or number < 0:
                return True
    return False


def validate_recovery_candidate(
    inventory: dict[str, Any] | None,
    total_asset: float,
    *,
    previous_total_asset: float = 0.0,
    rejected_rows: int = 0,
    pending_rows: int = 0,
    quotes_complete: bool = True,
) -> dict[str, Any]:
    """Validate a rebuilt candidate without deciding how it is persisted.

    The result is a non-financial control contract.  It intentionally returns
    no holdings or amounts so it can be written to a private diagnostic
    summary and safely inspected by Actions reviewers.
    """
    try:
        total = float(total_asset)
        previous = float(previous_total_asset or 0)
    except (TypeError, ValueError):
        return {"ready": False, "reasonCode": "BLOCKED_RECOVERY_INVALID", "checks": {"numeric": False}}
    checks = {
        "numeric": math.isfinite(total) and math.isfinite(previous),
        "inventory": isinstance(inventory, dict),
        "positiveAssets": inventory_has_positive_assets(inventory),
        "nonNegativeAssets": isinstance(inventory, dict) and not _has_invalid_asset_number(inventory),
        "quotesComplete": bool(quotes_complete),
        "noRejectedRows": int(rejected_rows or 0) == 0,
        "noPendingRows": int(pending_rows or 0) == 0,
    }
    if checks["numeric"] and previous > 0 and total <= 0:
        reason = "BLOCKED_ZERO_RESULT"
    elif not checks["numeric"] or not checks["inventory"] or not checks["nonNegativeAssets"]:
        reason = "BLOCKED_RECOVERY_INVALID"
    elif not checks["positiveAssets"]:
        reason = "BLOCKED_ZERO_RESULT"
    elif not checks["quotesComplete"] or not checks["noRejectedRows"] or not checks["noPendingRows"]:
        reason = "BLOCKED_RECOVERY_INVALID"
    else:
        reason = None
    checks["ready"] = reason is None and total > 0
    return {"ready": checks["ready"], "reasonCode": reason, "checks": checks}


__all__ = ["ASSET_BUCKETS", "inventory_has_positive_assets", "validate_recovery_candidate"]
