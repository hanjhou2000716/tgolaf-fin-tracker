"""Canonical parser for the clean Form V3 transaction contract.

Form V3 deliberately separates domain fields instead of asking the parser to
interpret a natural-language string.  This module is the only current-form
parser; the older compact/V2 parsers remain available for read-only migration
of archived response sheets.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import re
from uuid import NAMESPACE_URL, uuid5

from transaction_schema import (
    Action,
    RejectedTransaction,
    Transaction,
    TransactionParseResult,
    _normalize_header,
    _parse_date,
)


FORM_V3_SCHEMA = "FORM_V3"

FORM_V3_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "subject": ("交易主體", "交易主体", "subject", "transaction_subject"),
    "symbol": ("交易標的", "交易标的", "標的代號", "symbol", "ticker"),
    "action": ("交易動作", "交易动作", "action", "transaction_action"),
    "unit": ("交易單位", "交易单位", "unit", "transaction_unit"),
    "quantity": ("交易數量", "交易数量", "quantity", "transaction_quantity"),
    "timestamp": ("Timestamp", "時間戳記", "提交時間", "timestamp"),
    "email": ("Email Address", "電子郵件地址", "Email", "email"),
}

FORM_V3_BUSINESS_FIELDS = ("subject", "symbol", "action", "unit", "quantity")
FORM_V3_SUBJECTS = frozenset({"台股", "美股", "台幣", "美金", "質押", "擔保品"})

_ACTION_ALIASES = {
    "買入": Action.BUY,
    "買進": Action.BUY,
    "buy": Action.BUY,
    "賣出": Action.SELL,
    "賣": Action.SELL,
    "sell": Action.SELL,
    "存入": Action.DEPOSIT,
    "入金": Action.DEPOSIT,
    "deposit": Action.DEPOSIT,
    "提領": Action.WITHDRAWAL,
    "提款": Action.WITHDRAWAL,
    "提取": Action.WITHDRAWAL,
    "withdrawal": Action.WITHDRAWAL,
    "withdraw": Action.WITHDRAWAL,
    "全數取代": Action.SET_BALANCE,
    "全部取代": Action.SET_BALANCE,
    "取代": Action.SET_BALANCE,
    "setbalance": Action.SET_BALANCE,
    "借款": Action.BORROW,
    "借入": Action.BORROW,
    "borrow": Action.BORROW,
    "還款": Action.REPAY,
    "償還": Action.REPAY,
    "repay": Action.REPAY,
    "利率": Action.SET_PLEDGE_RATE,
    "設定利率": Action.SET_PLEDGE_RATE,
    "setpledgerate": Action.SET_PLEDGE_RATE,
}

_UNIT_ALIASES = {
    "張": "LOT", "lot": "LOT", "lots": "LOT",
    "股": "SHARE", "share": "SHARE", "shares": "SHARE",
    "台幣": "TWD", "新台幣": "TWD", "twd": "TWD",
    "美金": "USD", "美元": "USD", "usd": "USD",
    "%": "PERCENT", "percent": "PERCENT",
}

_QUANTITY_RE = re.compile(r"^\d+(?:\.\d+)?$")
_TW_SYMBOL_RE = re.compile(r"^\d{4,6}$")
_US_SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.]{0,9}$")


def _mapping(headers) -> dict[str, tuple[int, ...]]:
    normalized: dict[str, list[int]] = {}
    for index, value in enumerate(headers):
        normalized.setdefault(_normalize_header(value), []).append(index)
    mapping: dict[str, tuple[int, ...]] = {}
    for field, aliases in FORM_V3_HEADER_ALIASES.items():
        indexes: list[int] = []
        for alias in aliases:
            indexes.extend(normalized.get(_normalize_header(alias), []))
        if indexes:
            mapping[field] = tuple(dict.fromkeys(indexes))
    return mapping


def is_form_v3_headers(headers) -> bool:
    """Identify V3 from the five business headers only.

    Timestamp and Email Address are transport metadata.  Their presence or
    absence must never change the schema identity.
    """
    mapping = _mapping(headers)
    return all(field in mapping for field in FORM_V3_BUSINESS_FIELDS)


def form_v3_header_mapping(headers) -> dict[str, tuple[int, ...]]:
    """Expose the fixed header mapping for diagnostics and tests."""
    return _mapping(headers)


def _row_value(row, mapping: dict[str, tuple[int, ...]], field: str) -> str:
    indexes = mapping.get(field, ())
    values = [str(row[index]).strip() for index in indexes if index < len(row) and str(row[index]).strip()]
    if len(values) > 1:
        raise ValueError(f"DUPLICATE_{field.upper()}_AMBIGUOUS")
    return values[0] if values else ""


def _subject(value: str) -> str:
    raw = str(value or "").strip()
    aliases = {"台灣股市": "台股", "臺股": "台股", "臺幣": "台幣", "臺灣股": "台股"}
    normalized = aliases.get(raw, raw)
    if normalized not in FORM_V3_SUBJECTS:
        raise ValueError("INVALID_SUBJECT:交易主體必須是台股、美股、台幣、美金、質押或擔保品")
    return normalized


def _action(value: str) -> Action:
    key = _normalize_header(value)
    action = _ACTION_ALIASES.get(key)
    if action is None:
        raise ValueError("INVALID_ACTION:交易動作不在允許清單")
    return action


def _unit(value: str) -> str:
    key = _normalize_header(value)
    unit = _UNIT_ALIASES.get(key)
    if unit is None:
        raise ValueError("INVALID_UNIT:交易單位必須是張、股、台幣、美金或 %")
    return unit


def _quantity(value: str, *, allow_zero: bool) -> Decimal:
    raw = str(value or "").strip()
    if not _QUANTITY_RE.fullmatch(raw):
        raise ValueError("INVALID_QUANTITY:交易數量只能填非負數字")
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError) as error:
        raise ValueError("INVALID_QUANTITY:交易數量必須是有效數字") from error
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise ValueError("INVALID_QUANTITY:一般交易數量必須大於 0，取代可填 0")
    return parsed


def _symbol(subject: str, raw: str) -> str:
    value = str(raw or "").strip().upper()
    requires_symbol = subject in {"台股", "美股", "擔保品"}
    if requires_symbol and not value:
        raise ValueError("SYMBOL_REQUIRED:此交易主體必須填寫交易標的")
    if not requires_symbol and value:
        raise ValueError("SYMBOL_MUST_BE_EMPTY:台幣、美金與質押的交易標的必須留空")
    if not value:
        return ""
    if subject == "台股" and not _TW_SYMBOL_RE.fullmatch(value):
        raise ValueError("INVALID_SYMBOL:台股交易標的必須是 4 至 6 碼數字代號")
    if subject == "美股" and not _US_SYMBOL_RE.fullmatch(value):
        raise ValueError("INVALID_SYMBOL:美股交易標的必須是英文字母股票代號")
    if subject == "擔保品" and not (_TW_SYMBOL_RE.fullmatch(value) or _US_SYMBOL_RE.fullmatch(value)):
        raise ValueError("INVALID_SYMBOL:擔保品交易標的必須是股票代號")
    return value


def _canonical_fields(subject: str, symbol: str, action: Action, unit: str, quantity: Decimal):
    if subject == "台股":
        allowed_actions, allowed_units = {Action.BUY, Action.SELL, Action.SET_BALANCE}, {"LOT", "SHARE"}
        asset_type, canonical_symbol, currency = "台股", symbol, "TWD"
    elif subject == "美股":
        allowed_actions, allowed_units = {Action.BUY, Action.SELL, Action.SET_BALANCE}, {"SHARE"}
        asset_type, canonical_symbol, currency = "美股", symbol, "USD"
    elif subject in {"台幣", "美金"}:
        allowed_actions, allowed_units = {Action.DEPOSIT, Action.WITHDRAWAL, Action.SET_BALANCE}, {"TWD" if subject == "台幣" else "USD"}
        currency = "TWD" if subject == "台幣" else "USD"
        asset_type, canonical_symbol = f"現金_{currency}", currency
    elif subject == "質押":
        if action == Action.SET_PLEDGE_RATE:
            allowed_actions, allowed_units = {Action.SET_PLEDGE_RATE}, {"PERCENT"}
            asset_type, canonical_symbol, currency = "質押利率", "Rate", "TWD"
        else:
            allowed_actions, allowed_units = {Action.BORROW, Action.REPAY}, {"TWD"}
            asset_type, canonical_symbol, currency = "質押負債", "Current_Debt", "TWD"
    else:  # 擔保品 is intentionally independent from 質押.
        allowed_actions, allowed_units = {Action.DEPOSIT, Action.WITHDRAWAL}, {"LOT", "SHARE"}
        asset_type, canonical_symbol = "擔保品", symbol
        currency = "TWD" if _TW_SYMBOL_RE.fullmatch(symbol) else "USD"
    if action not in allowed_actions:
        raise ValueError(f"INVALID_ACTION_FOR_SUBJECT:{subject}只允許指定的交易動作")
    if unit not in allowed_units:
        raise ValueError(f"INVALID_UNIT_FOR_SUBJECT:{subject}不允許此交易單位")
    if unit == "LOT":
        quantity *= Decimal(1000)
        unit = "SHARE"
    return asset_type, canonical_symbol, currency, unit, quantity


def _accepted_row(transaction_date: date, asset_type: str, symbol: str, action: Action, quantity: Decimal) -> tuple[str, ...]:
    mode = {
        Action.BUY: "買入", Action.SELL: "賣出", Action.DEPOSIT: "存入",
        Action.WITHDRAWAL: "提領", Action.BORROW: "存入", Action.REPAY: "提領",
        Action.SET_BALANCE: "取代", Action.SET_PLEDGE_RATE: "取代",
    }[action]
    return (transaction_date.isoformat(), asset_type, symbol, mode, str(quantity))


def parse_form_v3_rows(
    headers,
    rows,
    *,
    source_sheet: str,
    existing_ids: set[str] | None = None,
) -> TransactionParseResult:
    """Parse V3 rows with fail-closed subject/action/unit validation."""
    mapping = _mapping(headers)
    if not is_form_v3_headers(headers):
        raise ValueError("FORM_V3 requires all five business headers")
    seen = set(existing_ids or ())
    accepted: list[Transaction] = []
    pending: list[Transaction] = []
    rejected: list[RejectedTransaction] = []
    accepted_rows: list[tuple[str, ...]] = []
    for row_number, raw_row in enumerate(rows, start=2):
        row = tuple(str(value) for value in raw_row)
        source_row_id = f"{source_sheet}:{row_number}"
        transaction_id = str(uuid5(NAMESPACE_URL, f"form-v3:{source_row_id}"))
        try:
            if transaction_id in seen:
                raise ValueError("DUPLICATE_TRANSACTION_ID:來源列已處理")
            submitted_at = _row_value(row, mapping, "timestamp")
            if not submitted_at:
                raise ValueError("INVALID_TIMESTAMP:缺少 Google Form Timestamp")
            email = _row_value(row, mapping, "email") or "form-v3@local.invalid"
            transaction_date = _parse_date(submitted_at)
            subject = _subject(_row_value(row, mapping, "subject"))
            symbol = _symbol(subject, _row_value(row, mapping, "symbol"))
            action = _action(_row_value(row, mapping, "action"))
            unit = _unit(_row_value(row, mapping, "unit"))
            quantity = _quantity(_row_value(row, mapping, "quantity"), allow_zero=action == Action.SET_BALANCE)
            asset_type, canonical_symbol, currency, canonical_unit, quantity = _canonical_fields(subject, symbol, action, unit, quantity)
            transaction = Transaction(
                transaction_id=transaction_id,
                source_row_id=source_row_id,
                submitted_at=submitted_at,
                submitter_email=email,
                approved=True,
                transaction_date=transaction_date,
                asset_type=asset_type,
                symbol=canonical_symbol,
                action=action,
                quantity=quantity,
                unit=canonical_unit,
                currency=currency,
            )
            accepted.append(transaction)
            accepted_rows.append(_accepted_row(transaction_date, asset_type, canonical_symbol, action, quantity))
            seen.add(transaction_id)
        except (ValueError, InvalidOperation) as error:
            message = str(error)
            reason, _, detail = message.partition(":")
            rejected.append(RejectedTransaction(source_row_id, reason or "INVALID_TRANSACTION", detail or message))
    return TransactionParseResult(tuple(accepted), tuple(pending), tuple(rejected), tuple(accepted_rows))


__all__ = [
    "FORM_V3_SCHEMA",
    "FORM_V3_HEADER_ALIASES",
    "FORM_V3_BUSINESS_FIELDS",
    "form_v3_header_mapping",
    "is_form_v3_headers",
    "parse_form_v3_rows",
]
