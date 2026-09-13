"""Shared non-financial contracts used by the Growth services.

Keeping these values in one small dependency prevents the private dashboard,
public status contract, and health watchdog from drifting apart. Strategy,
portfolio, and notification scheduling rules remain outside this module.
"""

from __future__ import annotations

import datetime


# Telegram button label shared by settlement and health-watchdog messages.
GROWTH_BUTTON_TEXT = "🌱 SFC.e Growth"

# Growth is updated only after the settlement windows. A 72-hour tolerance
# avoids treating the normal weekend gap as a stale-data incident. Skynet
# intentionally keeps its own 18-hour fallback in health_check.py.
GROWTH_STALE_AFTER_HOURS = 72

# One absolute-time contract shared by pipeline, API payloads and watchdog.
# Keep the fixed offset name for Python 3.10 compatibility; all persisted
# values are converted to UTC RFC3339 before they cross a service boundary.
UTC = datetime.timezone.utc
TAIPEI = datetime.timezone(datetime.timedelta(hours=8), name="Asia/Taipei")
FUTURE_TIMESTAMP_TOLERANCE = datetime.timedelta(minutes=5)


def parse_contract_timestamp(value, *, naive_timezone=TAIPEI):
    """Parse an ISO/RFC3339 timestamp, treating legacy naive values safely."""
    if value in (None, ""):
        raise ValueError("timestamp is missing")
    parsed = datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_timezone)
    return parsed


def rfc3339_utc(value):
    """Return an explicit-offset UTC timestamp for persisted/API fields."""
    parsed = value if isinstance(value, datetime.datetime) else parse_contract_timestamp(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TAIPEI)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")

