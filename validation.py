"""Fail-fast validation for Sheet-derived portfolio input."""

import math


REQUIRED_INVENTORY_KEYS = {
    "台股", "美股", "基金", "現金_TWD", "現金_USD", "質押負債", "質押利率", "擔保品"
}
REQUIRED_HISTORY_COLUMNS = {"Date", "Total_Asset", "Net_Asset", "Total_Debt"}


def _number(value, label, minimum=0):
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be numeric") from error
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{label} must be a finite value >= {minimum}")
    return result


def validate_inventory(inventory):
    if not inventory:
        raise ValueError("No portfolio records were parsed from Google Sheets")
    missing = REQUIRED_INVENTORY_KEYS - inventory.keys()
    if missing:
        raise ValueError(f"Required asset categories are missing: {', '.join(sorted(missing))}")

    _number(inventory["現金_TWD"].get("TWD"), "現金_TWD.TWD")
    _number(inventory["現金_USD"].get("USD"), "現金_USD.USD")
    _number(inventory["質押負債"].get("Current_Debt"), "質押負債.Current_Debt")
    rate = _number(inventory["質押利率"].get("Rate"), "質押利率.Rate")
    if rate > 30:
        raise ValueError("質押利率.Rate cannot exceed 30%")

    for category in ("台股", "美股", "基金", "擔保品"):
        for symbol, amount in inventory[category].items():
            if symbol != "History":
                _number(amount, f"{category}.{symbol}")
    return True


def validate_history_sheet(history_sheet):
    if history_sheet is None:
        raise ValueError("History worksheet is required for performance tracking")
    header = set(history_sheet.row_values(1))
    missing = REQUIRED_HISTORY_COLUMNS - header
    if missing:
        raise ValueError(f"History worksheet is missing columns: {', '.join(sorted(missing))}")
    return True


def validate_quote(symbol, price):
    return _number(price, f"Market quote for {symbol}", minimum=0.000001)
