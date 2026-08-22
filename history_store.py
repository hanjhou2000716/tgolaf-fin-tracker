"""Safe, key-based writes for the Google Sheets History worksheet.

The worksheet is user-editable, so column order must not be treated as an API.
This module keeps updates scoped to the named fields and leaves marker or
operator-maintained columns untouched.
"""

import json


def column_to_a1(column_index: int) -> str:
    """Convert a one-based column number to an A1 column label."""
    if not isinstance(column_index, int) or column_index < 1:
        raise ValueError("column_index must be a positive integer")
    label = ""
    value = column_index
    while value:
        value, remainder = divmod(value - 1, 26)
        label = chr(65 + remainder) + label
    return label


def build_header_map(headers):
    """Return a stripped header -> one-based column map; reject duplicates."""
    header_map = {}
    for index, raw_header in enumerate(headers, start=1):
        header = str(raw_header).strip()
        if not header:
            continue
        if header in header_map:
            raise ValueError(f"History worksheet has duplicate column: {header}")
        header_map[header] = index
    return header_map


def ensure_history_columns(history_sheet, columns):
    """Append missing named columns and return the refreshed header map."""
    if history_sheet is None:
        return {}
    headers = list(history_sheet.row_values(1))
    header_map = build_header_map(headers)
    for column in columns:
        name = str(column).strip()
        if not name:
            continue
        if name not in header_map:
            next_index = len(headers) + 1
            history_sheet.update_cell(1, next_index, name)
            headers.append(name)
            header_map[name] = next_index
    return header_map


def find_row_by_key(history_sheet, key, value):
    """Find the last data row whose named key equals ``value``."""
    if history_sheet is None:
        return None
    headers = history_sheet.row_values(1)
    header_map = build_header_map(headers)
    if key not in header_map:
        return None
    column_index = header_map[key] - 1
    expected = str(value).strip()[:10]
    rows = history_sheet.get_all_values()
    for row_number in range(len(rows), 1, -1):
        row = rows[row_number - 1]
        if len(row) > column_index and str(row[column_index]).strip()[:10] == expected:
            return row_number
    return None


def upsert_history_snapshot(history_sheet, values):
    """Create/update one snapshot using column names rather than positions.

    Only keys supplied in ``values`` are written on an update. This is
    intentional: durable Telegram markers and future user-managed columns are
    not part of a snapshot and must survive recalculation.
    """
    if history_sheet is None:
        return "skipped"
    if not isinstance(values, dict) or not values.get("Date"):
        raise ValueError("History snapshot must include a Date")

    headers = list(history_sheet.row_values(1))
    header_map = build_header_map(headers)
    for key in values:
        if key not in header_map:
            next_index = len(headers) + 1
            history_sheet.update_cell(1, next_index, key)
            headers.append(key)
            header_map[key] = next_index

    row_number = find_row_by_key(history_sheet, "Date", values["Date"])
    if row_number is not None:
        for key, value in values.items():
            column = column_to_a1(header_map[key])
            history_sheet.update(f"{column}{row_number}", [[value]])
        return "updated"

    row = [""] * len(headers)
    for key, value in values.items():
        row[header_map[key] - 1] = value
    history_sheet.append_row(row)
    return "created"


def ledger_conflict_alert_sent(history_sheet, snapshot_date, digest):
    """Return whether a conflict digest was already notified recently.

    The marker is written on the current History row, but an unchanged
    conflict can persist across settlement dates. Scanning recent marker cells
    prevents a daily duplicate Telegram alert while keeping a changed digest
    actionable. ``snapshot_date`` remains in the signature for compatibility
    with existing callers.
    """
    if history_sheet is None or not digest:
        return False
    header_map = build_header_map(history_sheet.row_values(1))
    marker_column = header_map.get("Ledger_Conflict_Alert_Marker")
    if not marker_column:
        return False
    rows = history_sheet.get_all_values()
    for row in reversed(rows[-60:]):
        raw_marker = str(row[marker_column - 1]).strip() if len(row) >= marker_column else ""
        if not raw_marker:
            continue
        try:
            markers = json.loads(raw_marker)
        except json.JSONDecodeError:
            continue
        if isinstance(markers, dict) and digest in markers:
            return True
    return False


def mark_ledger_conflict_alert_sent(history_sheet, snapshot_date, digest, sent_at):
    """Persist only a bounded conflict digest on the current History row."""
    if history_sheet is None or not digest:
        return
    header_map = build_header_map(history_sheet.row_values(1))
    marker_column = header_map.get("Ledger_Conflict_Alert_Marker")
    if not marker_column:
        return
    row_number = find_row_by_key(history_sheet, "Date", snapshot_date)
    if row_number is None:
        return
    row = history_sheet.row_values(row_number)
    raw_marker = str(row[marker_column - 1]).strip() if len(row) >= marker_column else ""
    try:
        markers = json.loads(raw_marker) if raw_marker else {}
    except json.JSONDecodeError:
        markers = {}
    if not isinstance(markers, dict):
        markers = {}
    markers[digest] = sent_at
    markers = dict(list(markers.items())[-20:])
    history_sheet.update(
        f"{column_to_a1(marker_column)}{row_number}",
        [[json.dumps(markers, ensure_ascii=False, separators=(",", ":"))]],
    )
