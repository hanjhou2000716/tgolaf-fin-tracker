"""Parser for the three-question, one-page Google Form.

The public form intentionally keeps only ``交易類型``, ``交易單位`` and
``交易數量`` as user-entered questions.  This module converts that compact
input into the canonical immutable transaction object while leaving the
legacy and Form V2 parsers untouched.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from uuid import NAMESPACE_URL, uuid5

from transaction_schema import (
    Action,
    RejectedTransaction,
    Transaction,
    TransactionParseResult,
    _normalize_header,
    _parse_bool,
    _parse_date,
)


SIMPLE_HEADER_ALIASES = {
    "timestamp": ("Timestamp", "提交時間", "時間戳記"),
    "email": ("Email Address", "Email", "提交者 Email"),
    "transaction_type": ("交易類型", "交易類型（標的＋動作）", "simple_transaction_type"),
    "unit": ("交易單位", "transaction_unit"),
    "quantity": ("交易數量", "transaction_quantity"),
    "approved": ("approved", "核准", "審核狀態"),
}

_ACTION_ALIASES = {
    "買入": Action.BUY,
    "買進": Action.BUY,
    "購入": Action.BUY,
    "buy": Action.BUY,
    "賣出": Action.SELL,
    "賣": Action.SELL,
    "sell": Action.SELL,
    "存入": Action.DEPOSIT,
    "入金": Action.DEPOSIT,
    "deposit": Action.DEPOSIT,
    "提領": Action.WITHDRAWAL,
    "提款": Action.WITHDRAWAL,
    "取出": Action.WITHDRAWAL,
    "withdraw": Action.WITHDRAWAL,
    "借款": Action.BORROW,
    "借入": Action.BORROW,
    "borrow": Action.BORROW,
    "還款": Action.REPAY,
    "償還": Action.REPAY,
    "還借款": Action.REPAY,
    "repay": Action.REPAY,
    "利率": Action.SET_PLEDGE_RATE,
    "設定利率": Action.SET_PLEDGE_RATE,
    "setpledgerate": Action.SET_PLEDGE_RATE,
    "取代": Action.SET_BALANCE,
    "覆蓋": Action.SET_BALANCE,
    "更新": Action.SET_BALANCE,
    "setbalance": Action.SET_BALANCE,
}

_UNIT_ALIASES = {
    "張": "LOT",
    "lot": "LOT",
    "lots": "LOT",
    "股": "SHARE",
    "share": "SHARE",
    "shares": "SHARE",
    "台幣": "TWD",
    "新台幣": "TWD",
    "twd": "TWD",
    "元": "TWD",
    "美金": "USD",
    "美元": "USD",
    "usd": "USD",
    "%": "PERCENT",
    "percent": "PERCENT",
}

_QUANTITY_RE = re.compile(r"^\d+(?:\.\d+)?$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,14}$")


def _mapping(headers) -> dict[str, int]:
    normalized = {}
    for index, value in enumerate(headers):
        normalized.setdefault(_normalize_header(value), []).append(index)
    result: dict[str, tuple[int, ...]] = {}
    for field, aliases in SIMPLE_HEADER_ALIASES.items():
        for alias in aliases:
            indexes = normalized.get(_normalize_header(alias))
            if indexes:
                result[field] = tuple(indexes)
                break
    return result


def is_simple_form_headers(headers) -> bool:
    """Return true for the explicit three-question response shape.

    Email Address is transport metadata and may be absent on an older Form;
    a separate, explicitly enabled recovery flag controls whether such rows
    may be accepted.
    """
    mapping = _mapping(headers)
    required = ("timestamp", "transaction_type", "unit", "quantity")
    if not all(field in mapping for field in required):
        return False
    normalized = {_normalize_header(value) for value in headers}
    # Explicit question labels identify the current branch even when the
    # response sheet still retains older V2 columns beside it.
    if _normalize_header("交易單位") in normalized and _normalize_header("交易數量") in normalized:
        return True
    v2_markers = ("市場", "資產代號", "價格", "幣別", "交易日期", "金額", "market", "symbol", "price", "currency")
    return not any(_normalize_header(marker) in normalized for marker in v2_markers)


def _value(row, mapping: dict[str, int], field: str) -> str:
    indexes = mapping.get(field, ())
    if isinstance(indexes, int):
        indexes = (indexes,)
    for index in indexes:
        if index < len(row) and str(row[index]).strip():
            return str(row[index]).strip()
    return ""


def _number(value: str, *, allow_zero: bool) -> Decimal:
    if not _QUANTITY_RE.fullmatch(value):
        raise ValueError("交易數量只能填數字")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise ValueError("交易數量必須是有效數字") from error
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise ValueError("交易數量必須是正數")
    return parsed


def _parse_type(value: str) -> tuple[str, Action]:
    parts = re.split(r"\s+", str(value or "").strip(), maxsplit=1)
    if len(parts) != 2 or not all(parts):
        raise ValueError("交易類型格式必須是『標的 動作』")
    subject, raw_action = parts[0].strip(), _normalize_header(parts[1])
    action = _ACTION_ALIASES.get(raw_action)
    if action is None:
        raise ValueError(f"不支援的交易動作: {parts[1]}")
    if subject in {"現金", "cash"}:
        subject = "現金"
    elif subject in {"質押", "擔保品", "pledge"}:
        subject = "質押"
    elif not _SYMBOL_RE.fullmatch(subject):
        raise ValueError("標的必須是股票／基金代號、現金或質押")
    return subject.upper() if subject not in {"現金", "質押"} else subject, action


def _asset_details(subject: str, action: Action, unit: str) -> tuple[str, str, str]:
    if subject == "現金":
        if action not in {Action.DEPOSIT, Action.WITHDRAWAL, Action.SET_BALANCE}:
            raise ValueError("現金只允許存入、提領或取代")
        if unit not in {"TWD", "USD"}:
            raise ValueError("現金交易單位必須是台幣或美金")
        return f"現金_{unit}", unit, unit
    if subject == "質押":
        if action == Action.SET_PLEDGE_RATE:
            if unit != "PERCENT":
                raise ValueError("質押利率的交易單位必須是 %")
            return "質押利率", "Rate", "TWD"
        if action not in {Action.BORROW, Action.REPAY, Action.SET_BALANCE}:
            raise ValueError("質押只允許借款、還款、利率或取代")
        if unit not in {"TWD", "USD"}:
            raise ValueError("質押金額單位必須是台幣或美金")
        return "質押負債", "Current_Debt", unit
    if action not in {Action.BUY, Action.SELL, Action.SET_BALANCE}:
        raise ValueError("股票／基金只允許買入、賣出或取代")
    if unit not in {"LOT", "SHARE"}:
        raise ValueError("股票／基金交易單位必須是張或股")
    if subject[0].isdigit():
        return "台股", subject, "TWD"
    return "美股", subject, "USD"


def _accepted_row(transaction_date, asset_type: str, symbol: str, action: Action, quantity: Decimal) -> tuple[str, ...]:
    mode = {
        Action.BUY: "買入",
        Action.SELL: "賣出",
        Action.DEPOSIT: "存入",
        Action.WITHDRAWAL: "提領",
        Action.BORROW: "存入",
        Action.REPAY: "提領",
        Action.SET_BALANCE: "取代",
        Action.SET_PLEDGE_RATE: "取代",
    }[action]
    return (transaction_date.isoformat(), asset_type, symbol, mode, str(quantity))


def parse_simple_transaction_rows(
    headers,
    rows,
    *,
    source_sheet: str,
    existing_ids: set[str] | None = None,
    allow_missing_email_compat: bool = False,
) -> TransactionParseResult:
    """Parse one-page form responses into canonical transactions."""
    mapping = _mapping(headers)
    if not is_simple_form_headers(headers):
        raise ValueError("simple form headers are incomplete")
    seen = set(existing_ids or set())
    accepted: list[Transaction] = []
    pending: list[Transaction] = []
    rejected: list[RejectedTransaction] = []
    accepted_rows: list[tuple[str, ...]] = []
    for row_number, raw_row in enumerate(rows, start=2):
        row = tuple(str(value) for value in raw_row)
        source_row_id = f"{source_sheet}:{row_number}"
        transaction_id = str(uuid5(NAMESPACE_URL, source_row_id))
        try:
            if transaction_id in seen:
                raise ValueError("duplicate_transaction_id")
            submitted_at = _value(row, mapping, "timestamp")
            email = _value(row, mapping, "email")
            compatibility_used = None
            if not email and allow_missing_email_compat:
                email = "form-compatibility@local.invalid"
                compatibility_used = "current_simple_form_missing_email"
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                raise ValueError("submitter_email is invalid")
            transaction_date = _parse_date(submitted_at)
            subject, action = _parse_type(_value(row, mapping, "transaction_type"))
            raw_unit = _normalize_header(_value(row, mapping, "unit"))
            unit = _UNIT_ALIASES.get(raw_unit)
            if unit is None:
                raise ValueError("不支援的交易單位")
            quantity = _number(_value(row, mapping, "quantity"), allow_zero=action == Action.SET_BALANCE)
            asset_type, symbol, currency = _asset_details(subject, action, unit)
            if unit == "LOT":
                quantity *= Decimal(1000)
                unit = "SHARE"
            if action == Action.SET_PLEDGE_RATE:
                transaction_unit = "PERCENT"
            elif unit in {"TWD", "USD"}:
                transaction_unit = unit
            else:
                transaction_unit = "SHARE"
            approved = True
            if mapping.get("approved") is not None and _value(row, mapping, "approved"):
                approved = _parse_bool(_value(row, mapping, "approved"))
            transaction = Transaction(
                transaction_id=transaction_id,
                source_row_id=source_row_id,
                submitted_at=submitted_at,
                submitter_email=email,
                approved=approved,
                transaction_date=transaction_date,
                asset_type=asset_type,
                symbol=symbol,
                action=action,
                quantity=quantity,
                unit=transaction_unit,
                currency=currency,
                compatibility_used=compatibility_used,
            )
            seen.add(transaction_id)
            if not approved:
                pending.append(transaction)
            else:
                accepted.append(transaction)
                accepted_rows.append(_accepted_row(transaction_date, asset_type, symbol, action, quantity))
        except (ValueError, InvalidOperation) as error:
            rejected.append(RejectedTransaction(source_row_id, "invalid_transaction", str(error)))
    return TransactionParseResult(tuple(accepted), tuple(pending), tuple(rejected), tuple(accepted_rows))
