"""Command layer for the compact Form and append-only reconciliation ledger.

The parser is intentionally small and fail-closed.  A balance command changes
the observed cash balance to an explicit target; it is not a deposit and is
never classified as investment P&L or external cash flow.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from transaction_schema import Action, Transaction


class CommandValidationError(ValueError):
    """Raised when a command is missing an explicit, safe value."""


class CommandStatus:
    APPLIED = "APPLIED"
    PENDING = "PENDING"
    REJECTED = "REJECTED"
    APPLIED_WITH_COMPATIBILITY = "APPLIED_WITH_COMPATIBILITY"


@dataclass(frozen=True)
class TransactionCommand:
    transaction: Transaction
    status: str
    reason: str | None = None
    compatibility_used: str | None = None
    target_balance: Decimal | None = None

    def ingestion_payload(self) -> dict[str, Any]:
        tx = self.transaction
        return {
            "transactionId": tx.transaction_id,
            "sourceRowId": tx.source_row_id,
            "submittedAt": tx.submitted_at,
            "transactionDate": tx.transaction_date.isoformat(),
            "command": tx.action.value,
            "asset": tx.asset_type,
            "symbol": tx.symbol,
            "currency": tx.currency,
            "amount": str(tx.quantity),
            "targetBalance": str(self.target_balance) if self.target_balance is not None else None,
            "adjustment": str(tx.reconciliation_delta) if tx.reconciliation_delta is not None else None,
            "status": self.status,
            "reason": self.reason,
            "compatibilityUsed": self.compatibility_used,
        }


def _text(record: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError) as error:
        raise CommandValidationError(f"{field} must be numeric") from error
    if not parsed.is_finite() or parsed < 0:
        raise CommandValidationError(f"{field} must be finite and non-negative")
    return parsed


def _date(value: Any, fallback: Any = None) -> date:
    raw = _text({"value": value}, "value") or _text({"value": fallback}, "value")
    if not raw:
        return date.today()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(raw[:10].replace("/", "-"))
        except ValueError as error:
            raise CommandValidationError("transaction_date must be ISO date") from error


def _transaction_id(record: Mapping[str, Any], source_row_id: str) -> str:
    raw = _text(record, "transaction_id", "transactionId", "uuid")
    if raw:
        try:
            UUID(raw)
        except ValueError as error:
            raise CommandValidationError("transaction_id must be UUID") from error
        return raw
    if not source_row_id:
        raise CommandValidationError("source_row_id is required when transaction_id is omitted")
    return str(uuid5(NAMESPACE_URL, source_row_id))


def _is_balance_command(record: Mapping[str, Any]) -> bool:
    command = _text(record, "command", "action", "交易類型", "異動類型").upper().replace("-", "_").replace(" ", "")
    description = _text(record, "description", "transaction_text", "交易內容", "交易描述", "備註")
    if command in {Action.SET_BALANCE.value, "SETBALANCE", "設定餘額", "設定現金", "對帳", "餘額校正"}:
        return True
    return any(token in description.lower() for token in ("set_balance", "balance", "餘額", "對帳", "現金校正"))


def parse_set_balance_command(record: Mapping[str, Any]) -> TransactionCommand:
    """Parse an explicit SET_BALANCE row or an audited legacy row.

    Legacy compatibility is deliberately narrow: only a cash/balance row with
    an old ``price``/``價格`` field is adapted, and the result is visibly
    labelled ``APPLIED_WITH_COMPATIBILITY``.
    """
    if not _is_balance_command(record):
        raise CommandValidationError("command is not SET_BALANCE")
    source_row_id = _text(record, "source_row_id", "sourceRowId", "row_id")
    if not source_row_id:
        raise CommandValidationError("source_row_id is required")
    currency = _text(record, "currency", "幣別").upper()
    if currency not in {"TWD", "USD"}:
        raise CommandValidationError("SET_BALANCE currency must be TWD or USD")
    asset_type = _text(record, "asset_type", "assetType", "資產類別")
    symbol = _text(record, "symbol", "標的", "資產代號") or currency
    is_cash = currency == symbol.upper() or asset_type.lower().startswith(("現金", "cash"))
    if not is_cash:
        raise CommandValidationError("SET_BALANCE only supports an explicit cash asset")

    target_raw = _text(record, "target_balance", "targetBalance", "target_amount", "targetAmount", "目標餘額", "目標現金")
    compatibility = None
    if not target_raw:
        target_raw = _text(record, "amount", "quantity", "數量", "金額")
    if not target_raw:
        legacy_price = _text(record, "price", "legacy_price", "價格", "成交價")
        description = _text(record, "description", "transaction_text", "交易內容", "備註")
        if legacy_price and any(token in description.lower() for token in ("balance", "餘額", "現金", "對帳")):
            target_raw = legacy_price
            compatibility = "legacy_target_from_price_field"
    if not target_raw:
        raise CommandValidationError("SET_BALANCE target_balance is required")
    target = _decimal(target_raw, "target_balance")
    submitted_at = _text(record, "submitted_at", "submittedAt", "timestamp", "Timestamp")
    approved_raw = _text(record, "approved", "核准", "審核狀態").lower()
    approved = approved_raw not in {"false", "0", "no", "否", "pending", "待確認"}
    status = CommandStatus.APPLIED_WITH_COMPATIBILITY if compatibility and approved else CommandStatus.APPLIED if approved else CommandStatus.PENDING
    tx = Transaction(
        transaction_id=_transaction_id(record, source_row_id),
        source_row_id=source_row_id,
        submitted_at=submitted_at,
        submitter_email=_text(record, "submitter_email", "submitterEmail", "email", "Email Address"),
        approved=approved,
        transaction_date=_date(record.get("transaction_date") or record.get("transactionDate"), submitted_at),
        asset_type=asset_type or ("現金_TWD" if currency == "TWD" else "現金_USD"),
        symbol=symbol,
        action=Action.SET_BALANCE,
        quantity=target,
        unit=currency,
        currency=currency,
    )
    return TransactionCommand(tx, status, compatibility_used=compatibility, target_balance=target)


def command_from_transaction(transaction: Transaction) -> TransactionCommand:
    """Wrap an already validated transaction for ingestion/status rendering."""
    if transaction.action == Action.SET_BALANCE:
        status = CommandStatus.APPLIED_WITH_COMPATIBILITY if transaction.compatibility_used and transaction.approved else CommandStatus.APPLIED if transaction.approved else CommandStatus.PENDING
        return TransactionCommand(transaction, status, compatibility_used=transaction.compatibility_used, target_balance=transaction.quantity)
    return TransactionCommand(transaction, CommandStatus.APPLIED if transaction.approved else CommandStatus.PENDING)


def _is_cash_set_balance(transaction: Transaction) -> bool:
    """Return whether a SET_BALANCE belongs to the cash reconciliation path."""
    if transaction.action != Action.SET_BALANCE:
        return False
    asset_type = str(transaction.asset_type or "").strip().lower()
    # Explicit V2 cash rows use 現金_TWD／現金_USD.  Keep the symbol-only
    # fallback for old exports that omitted asset_type but had TWD/USD.
    return asset_type.startswith(("現金", "cash")) or (
        asset_type in {"", "twd", "usd"}
        and str(transaction.symbol or "").upper() in {"TWD", "USD"}
    )


def transaction_order_key(transaction: Transaction):
    """Sort by the full submitted timestamp, then numeric source row and ID."""
    raw = str(transaction.submitted_at or "").strip()
    parsed = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        parsed = parsed.astimezone(timezone.utc)
    except ValueError:
        parsed = None
    match = re.search(
        r"(\d{4})[/-](\d{1,2})[/-](\d{1,2}).*?(上午|下午|AM|PM)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?",
        raw,
        re.IGNORECASE,
    )
    if parsed is None and match:
        year, month, day, meridiem, hour, minute, second = match.groups()
    elif parsed is None:
        match = re.search(
            r"(\d{1,2})[/-](\d{1,2})[/-](\d{4}).*?(上午|下午|AM|PM)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?",
            raw,
            re.IGNORECASE,
        )
        if match:
            month, day, year, meridiem, hour, minute, second = match.groups()
        else:
            year = month = day = meridiem = hour = minute = second = None
    else:
        year = month = day = meridiem = hour = minute = second = None
    if parsed is None and year is not None:
        hour = int(hour)
        normalized_meridiem = str(meridiem or "").upper()
        if normalized_meridiem in {"下午", "PM"} and hour < 12:
            hour += 12
        elif normalized_meridiem in {"上午", "AM"} and hour == 12:
            hour = 0
        try:
            parsed = datetime(
                int(year), int(month), int(day), hour, int(minute), int(second or 0),
                tzinfo=timezone(timedelta(hours=8)),
            )
            parsed = parsed.astimezone(timezone.utc)
        except ValueError:
            parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
        except ValueError:
            parsed = datetime.combine(transaction.transaction_date, time.min, tzinfo=timezone.utc)
    source_row = str(transaction.source_row_id or "")
    row_match = re.search(r":(\d+)$", source_row)
    row_number = int(row_match.group(1)) if row_match else 0
    return parsed, row_number, source_row, str(transaction.transaction_id or "")


def build_ingestion_status(
    *, accepted=(), pending=(), rejected=(), compatibility=(), source_role="CURRENT"
) -> list[dict[str, Any]]:
    """Return the latest status rows without exposing submitter email."""
    rows: list[dict[str, Any]] = []
    rows.extend({**command_from_transaction(item).ingestion_payload(), "sourceRole": source_role} for item in accepted)
    for item in pending:
        marker = str(item.compatibility_used or "")
        if marker.startswith("settlement_quote_pending:"):
            detail = marker.partition(":")[2]
            reason_code, reason = (
                ("TRADE_DATE_QUOTE_MISMATCH", "找不到與交易日期相符的收盤價")
                if detail == "quote date does not match transaction date"
                else ("TRADE_DATE_QUOTE_UNAVAILABLE", "交易日期收盤價尚無法驗證")
            )
            stage = "price"
        else:
            reason_code, reason, stage = "APPROVAL_PENDING", "交易尚待核准", "approval"
        rows.append({
            **command_from_transaction(item).ingestion_payload(),
            "status": CommandStatus.PENDING,
            "sourceRole": source_role,
            "processingStage": stage,
            "reasonCode": reason_code,
            "reason": reason,
        })
    for item in rejected:
        detail = item.detail or item.reason
        rows.append({
            "sourceRowId": item.source_row_id,
            "submittedAt": item.submitted_at,
            "symbol": item.symbol,
            "status": CommandStatus.REJECTED,
            "reason": detail,
            "reasonCode": item.reason,
            "detail": detail,
            "processingStage": item.stage or "parse",
            "inputHash": item.input_hash,
            "sourceRole": source_role,
        })
    rows.extend(
        {**item.ingestion_payload(), "sourceRole": source_role}
        if isinstance(item, TransactionCommand)
        else {**item, "sourceRole": item.get("sourceRole", source_role)}
        for item in compatibility
    )
    return rows


def build_ingestion_contract(rows, *, schema="CURRENT", ingestion_health=None) -> dict[str, Any]:
    """Serialize status rows plus an explicit ingestion-health contract.

    ``recent`` remains backwards compatible for the Mini App.  The additional
    ``ingestionHealth`` object lets the dashboard distinguish a healthy
    pipeline from a pipeline that ran successfully but quarantined bad input.
    """
    rows = list(rows or [])
    current_rows = [row for row in rows if str(row.get("sourceRole") or "CURRENT").upper() == "CURRENT"]
    legacy_rows = [row for row in rows if str(row.get("sourceRole") or "CURRENT").upper() == "LEGACY_ARCHIVE"]

    def summarize(source_rows):
        return {
            "applied": sum(1 for row in source_rows if row.get("status") in {CommandStatus.APPLIED, CommandStatus.APPLIED_WITH_COMPATIBILITY}),
            "pending": sum(1 for row in source_rows if row.get("status") == CommandStatus.PENDING),
            "rejected": sum(1 for row in source_rows if row.get("status") == CommandStatus.REJECTED),
        }

    summary = summarize(current_rows)
    legacy_summary = summarize(legacy_rows)
    issues = [
        {
            key: row.get(key)
            for key in ("sourceRowId", "submittedAt", "symbol", "status", "reasonCode", "reason", "processingStage", "inputHash")
            if row.get(key) is not None
        }
        for row in current_rows
        if row.get("status") in {CommandStatus.PENDING, CommandStatus.REJECTED}
    ]
    health = {
        "status": "DEGRADED" if summary["rejected"] or summary["pending"] else "OK",
        "schema": schema,
        "accepted": summary["applied"],
        "rejected": summary["rejected"],
        "reasonCode": None,
        "message": None,
    }
    if ingestion_health:
        health.update({key: value for key, value in ingestion_health.items() if value is not None})
    recent = current_rows[-5:]
    result = {"summary": summary, "recent": recent, "issues": issues, "ingestionHealth": health}
    if legacy_rows:
        result["legacySummary"] = legacy_summary
    return result


def build_partial_update_message(ingestion_contract: dict[str, Any]) -> str:
    """Build a loss-only input incident without publishing partial P&L."""
    issues = ingestion_contract.get("issues") if isinstance(ingestion_contract, dict) else None
    lines = ["⚠️ Growth 資料部分更新"]
    for issue in (issues or [])[:5]:
        symbol = str(issue.get("symbol") or "未識別標的")
        source_row = str(issue.get("sourceRowId") or "")
        row_match = re.search(r":(\d+)$", source_row)
        row_label = f"（第 {row_match.group(1)} 列）" if row_match else ""
        reason_code = str(issue.get("reasonCode") or "TRADE_PENDING")
        reason_detail = str(issue.get("reason") or "交易待處理")
        reason = reason_code if reason_detail == reason_code else f"{reason_code}：{reason_detail}"
        lines.append(f"{symbol}{row_label}：交易待處理（{reason}）")
    if len(issues or []) > 5:
        lines.append(f"另有 {len(issues) - 5} 筆交易待處理")
    if len(lines) == 1:
        health = ingestion_contract.get("ingestionHealth", {}) if isinstance(ingestion_contract, dict) else {}
        lines.append(str(health.get("reasonCode") or "輸入資料尚未完整"))
    lines.append("損益待交易資料完整後確認。")
    return "\n".join(lines)


def inventory_rows_from_transactions(transactions, accepted_rows):
    """Build the legacy inventory stream without double-applying cash corrections.

    ``SET_BALANCE`` is the canonical command for cash reconciliation, so cash
    rows are applied exactly once by the chronological current/legacy
    reconciliation paths.
    Historical Form rows also used the old "replace" operation for securities,
    pledge debt and funds.  Those rows are snapshot baselines, not cash
    corrections, and must remain in the compatibility inventory stream or the
    migration would silently erase historical holdings and debt.
    """
    transactions = tuple(transactions or ())
    accepted_rows = tuple(accepted_rows or ())
    if len(transactions) != len(accepted_rows):
        raise ValueError("accepted transaction and legacy row counts must match")
    return tuple(
        row for transaction, row in zip(transactions, accepted_rows)
        if not _is_cash_set_balance(transaction)
    )


def apply_reconciliation_events(inventory: dict[str, dict[str, Any]], transactions):
    """Apply SET_BALANCE through an explicit delta and return immutable events.

    The legacy inventory adapter remains available for historical rows, but
    cash corrections go through this single compatibility boundary so the
    previous value, target and signed adjustment are all auditable.
    """
    updated = []
    events: list[dict[str, Any]] = []
    for transaction in sorted(transactions, key=transaction_order_key):
        if not _is_cash_set_balance(transaction):
            updated.append(transaction)
            continue
        currency = transaction.currency.upper()
        bucket_key = next((key for key in inventory if key.endswith(f"_{currency}")), None)
        if bucket_key is None:
            raise CommandValidationError(f"cash inventory bucket missing for {currency}")
        bucket = inventory[bucket_key]
        previous = Decimal(str(bucket.get(currency, 0)))
        target = Decimal(transaction.quantity)
        delta = target - previous
        bucket[currency] = float(target)
        adjusted = replace(transaction, reconciliation_delta=delta)
        updated.append(adjusted)
        events.append({
            "transactionId": transaction.transaction_id,
            "sourceRowId": transaction.source_row_id,
            "currency": currency,
            "previousBalance": str(previous),
            "targetBalance": str(target),
            "adjustment": str(delta),
            "eventType": "RECONCILIATION_INCREASE" if delta >= 0 else "RECONCILIATION_DECREASE",
        })
    return tuple(updated), events


def apply_current_transactions(inventory: dict[str, dict[str, Any]], transactions):
    """Apply current-form events in chronological order.

    Current V3 rows can mix a balance replacement with later deposits,
    withdrawals, or trades in the same response sheet.  Applying every
    ``SET_BALANCE`` after all cash flows makes the replacement erase those
    later flows.  This helper is deliberately small and deterministic: it
    applies each event once in timestamp/source-row order and returns the
    reconciliation deltas for cash replacements.
    """
    updated: list[Transaction] = []
    events: list[dict[str, Any]] = []

    def cash_bucket(currency: str) -> dict[str, Any]:
        currency = currency.upper().strip()
        return inventory.setdefault(f"現金_{currency}", {currency: 0.0})

    for transaction in sorted(tuple(transactions or ()), key=transaction_order_key):
        action = transaction.action
        asset_type = str(transaction.asset_type or "")
        symbol = str(transaction.symbol or "")
        currency = str(transaction.currency or "TWD").upper().strip()
        quantity = Decimal(transaction.quantity)

        if action == Action.SET_BALANCE:
            if _is_cash_set_balance(transaction):
                bucket = cash_bucket(currency)
                previous = Decimal(str(bucket.get(currency, 0) or 0))
                bucket[currency] = float(quantity)
                adjusted = replace(transaction, reconciliation_delta=quantity - previous)
                updated.append(adjusted)
                events.append({
                    "transactionId": transaction.transaction_id,
                    "sourceRowId": transaction.source_row_id,
                    "currency": currency,
                    "previousBalance": str(previous),
                    "targetBalance": str(quantity),
                    "adjustment": str(quantity - previous),
                    "eventType": "RECONCILIATION_INCREASE" if quantity >= previous else "RECONCILIATION_DECREASE",
                })
            else:
                bucket = inventory.setdefault(asset_type, {})
                if symbol:
                    bucket[symbol] = float(quantity)
                updated.append(transaction)
            continue

        if action in {Action.BUY, Action.SELL}:
            bucket = inventory.setdefault(asset_type, {})
            bucket[symbol] = float(bucket.get(symbol, 0) or 0) + float(quantity if action == Action.BUY else -quantity)
            if transaction.price is not None:
                cash = cash_bucket(currency)
                notional = quantity * Decimal(transaction.price)
                cash[currency] = float(Decimal(str(cash.get(currency, 0) or 0)) + (notional if action == Action.SELL else -notional))
        elif action in {Action.DEPOSIT, Action.WITHDRAWAL}:
            if asset_type == "擔保品":
                bucket = inventory.setdefault("擔保品", {})
                bucket[symbol] = float(bucket.get(symbol, 0) or 0) + float(quantity if action == Action.DEPOSIT else -quantity)
            else:
                cash = cash_bucket(currency)
                delta = quantity if action == Action.DEPOSIT else -quantity
                cash[currency] = float(Decimal(str(cash.get(currency, 0) or 0)) + delta)
        elif action in {Action.BORROW, Action.REPAY}:
            debt = inventory.setdefault("質押負債", {"Current_Debt": 0.0, "History": []})
            cash = cash_bucket(currency)
            delta = quantity if action == Action.BORROW else -quantity
            debt["Current_Debt"] = float(Decimal(str(debt.get("Current_Debt", 0) or 0)) + delta)
            cash[currency] = float(Decimal(str(cash.get(currency, 0) or 0)) + delta)
            debt.setdefault("History", []).append((transaction.transaction_date, debt["Current_Debt"]))
        elif action == Action.SET_PLEDGE_RATE:
            rate = inventory.setdefault("質押利率", {"Rate": 0.0, "History": []})
            rate["Rate"] = float(quantity)
            rate.setdefault("History", []).append((transaction.transaction_date, float(quantity)))
        updated.append(transaction)

    return tuple(updated), tuple(events)
