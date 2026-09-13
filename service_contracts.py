"""Shared non-financial contracts used by the Growth services.

Keeping these values in one small dependency prevents the private dashboard,
public status contract, and health watchdog from drifting apart. Strategy,
portfolio, and notification scheduling rules remain outside this module.
"""

from __future__ import annotations


# Telegram button label shared by settlement and health-watchdog messages.
GROWTH_BUTTON_TEXT = "🌱SFC.e Growth"

# Growth is updated only after the settlement windows. A 72-hour tolerance
# avoids treating the normal weekend gap as a stale-data incident. Skynet
# intentionally keeps its own 18-hour fallback in health_check.py.
GROWTH_STALE_AFTER_HOURS = 72

