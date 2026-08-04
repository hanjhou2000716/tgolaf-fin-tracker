"""Strict Google Form transaction contract and approval gate."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from typing import Sequence
from uuid import UUID


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    DIVIDEND = "DIVIDEND"
    INTEREST = "INTEREST"
    FEE = "FEE"
    TAX = "TAX"
    BORROW = "BORROW"
    REPAY = "REPAY"
    SPLIT = "SPLIT"
    SPIN_OFF = "SPIN_OFF"
    TRANSFER = "TRANSFER"
    FX_CONVERSION = "FX_CONVERSION"


class TransactionSchemaError(ValueError):
    """Raised when the Form header cannot satisfy the fixed contract."""


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    source_row_id: str
    submitted_at: str
    submitter_email: str
    approved: bool
    transaction_date: date
    asset_type: str
    symbol: str
    action: Action
    quantity: Decimal
    unit: str
    currency: str
    price: Decimal | None = None


@dataclass(frozen=True)
class RejectedTransaction:
    source_row_id: str
    reason: str
    detail: str


@dataclass(frozen=True)
class TransactionParseResult:
    accepted: tuple[Transaction, ...]
    pending: tuple[Transaction, ...]
    rejected: tuple[RejectedTransaction, ...]
    accepted_rows: tuple[tuple[str, ...], ...]

    def audit_payload(self) -> dict:
        pending = [
            {
                "transaction_id": item.transaction_id,
                "source_row_id": item.source_row_id,
                "submitter_email": item.submitter_email,
                "transaction_date": item.transaction_date.isoformat(),
                "asset_type": item.asset_type,
                "symbol": item.symbol,
                "action": item.action.value,
                "quantity": str(item.quantity),
                "unit": item.unit,
                "currency": item.currency,
            }
            for item in self.pending
        ]
        return {
            "accepted": len(self.accepted),
            "pending": pending,
            "rejected": [item.__dict__ for item in self.rejected],
        }


REQUIRED_FIELDS = (
    "transaction_id",
    "submitted_at",
    "submitter_email",
    "approved",
    "transaction_date",
    "asset_type",
    "symbol",
    "action",
    "quantity",
    "unit",
    "currency",
)

HEADER_ALIASES = {
    "transaction_id": ("transaction_id", "transaction id", "交易id", "交易編號", "uuid"),
    "submitted_at": ("timestamp", "submitted_at", "submitted at", "提交時間"),
    "submitter_email": ("email address", "email", "submitter_email", "提交者email", "提交者 email"),
    "approved": ("approved", "核准", "已核准", "審核狀態", "審核状态"),
    "transaction_date": ("transaction_date", "transaction date", "交易日期", "日期"),
    "asset_type": ("asset_type", "asset type", "資產類別", "資產類型"),
    "symbol": ("symbol", "標的", "股票代號", "代號"),
    "action": ("action", "操作", "交易類型", "異動類型"),
    "quantity": ("quantity", "數量", "金額", "交易數量"),
    "unit": ("unit", "單位"),
    "currency": ("currency", "幣別"),
    "price": ("price", "價格", "成交價"),
}


def _normalize_header(value) -> str:
    return re.sub(r"[\s:_\-（）()]+", "", str(value or "").strip().lower())


def resolve_headers(headers: Sequence[str]) -> dict[str, int]:
    normalized = {_normalize_header(value): index for index, value in enumerate(headers)}
    mapping = {}
    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            index = normalized.get(_normalize_header(alias))
            if index is not None:
                mapping[field] = index
                break
    missing = [field for field in REQUIRED_FIELDS if field not in mapping]
    if missing:
        raise TransactionSchemaError(f"Missing required transaction columns: {', '.join(missing)}")
    return mapping


def parse_quantity(value, unit) -> tuple[Decimal, str]:
    """Parse explicit units; Taiwan lots are normalized to individual shares."""
    try:
        number = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError) as error:
        raise ValueError("quantity must be numeric") from error
    if not number.is_finite() or number < 0:
        raise ValueError("quantity must be finite and non-negative")
    unit_key = _normalize_header(unit)
    aliases = {
        "股": "SHARE",
        "shares": "SHARE",
        "share": "SHARE",
        "張": "LOT",
        "lots": "LOT",
        "lot": "LOT",
        "元": "TWD",
        "twd": "TWD",
        "台幣": "TWD",
        "usd": "USD",
        "美元": "USD",
        "%": "PERCENT",
        "percent": "PERCENT",
    }
    canonical = aliases.get(unit_key)
    if not canonical:
        raise ValueError(f"unsupported unit: {unit}")
    if canonical == "LOT":
        return number * Decimal(1000), "SHARE"
    if canonical == "PERCENT":
        return number / Decimal(100), canonical
    return number, canonical


def _value(row: Sequence[str], mapping: dict[str, int], field: str) -> str:
    index = mapping[field]
    return str(row[index]).strip() if index < len(row) else ""


def _parse_bool(value: str) -> bool:
    normalized = _normalize_header(value)
    if normalized in {"true", "yes", "y", "1", "approved", "核准", "是"}:
        return True
    if normalized in {"false", "no", "n", "0", "pending", "待確認", "否"}:
        return False
    raise ValueError("approved must be an explicit boolean")


def _parse_date(value: str) -> date:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10].replace("/", "-"))
        except ValueError as error:
            raise ValueError("transaction_date must be ISO date") from error


def _parse_action(value: str) -> Action:
    normalized = _normalize_header(value).upper()
    aliases = {
        "買入": Action.BUY,
        "賣出": Action.SELL,
        "存入": Action.DEPOSIT,
        "提領": Action.WITHDRAWAL,
        "股息": Action.DIVIDEND,
        "利息": Action.INTEREST,
        "手續費": Action.FEE,
        "交易稅": Action.TAX,
        "借款": Action.BORROW,
        "還款": Action.REPAY,
        "分割": Action.SPLIT,
        "分拆": Action.SPIN_OFF,
        "轉移": Action.TRANSFER,
        "換匯": Action.FX_CONVERSION,
    }
    if normalized in aliases:
        return aliases[normalized]
    normalized = {"SPINOFF": "SPIN_OFF", "FXCONVERSION": "FX_CONVERSION"}.get(normalized, normalized)
    return Action(normalized)


def parse_transaction_rows(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    source_sheet: str,
    existing_ids: set[str] | None = None,
) -> TransactionParseResult:
    mapping = resolve_headers(headers)
    seen = set(existing_ids or set())
    accepted, pending, rejected, accepted_rows = [], [], [], []
    for row_number, raw_row in enumerate(rows, start=2):
        row = tuple(str(value) for value in raw_row)
        source_row_id = f"{source_sheet}:{row_number}"
        try:
            transaction_id = _value(row, mapping, "transaction_id")
            UUID(transaction_id)
            if transaction_id in seen:
                raise ValueError("duplicate_transaction_id")
            submitted_at = _value(row, mapping, "submitted_at")
            email = _value(row, mapping, "submitter_email")
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                raise ValueError("submitter_email is invalid")
            approved = _parse_bool(_value(row, mapping, "approved"))
            quantity, canonical_unit = parse_quantity(_value(row, mapping, "quantity"), _value(row, mapping, "unit"))
            price_text = _value(row, mapping, "price")
            price = Decimal(price_text.replace(",", "").replace("$", "")) if price_text else None
            if price is not None and (not price.is_finite() or price < 0):
                raise ValueError("price must be finite and non-negative")
            transaction = Transaction(
                transaction_id=transaction_id,
                source_row_id=source_row_id,
                submitted_at=submitted_at,
                submitter_email=email,
                approved=approved,
                transaction_date=_parse_date(_value(row, mapping, "transaction_date")),
                asset_type=_value(row, mapping, "asset_type"),
                symbol=_value(row, mapping, "symbol"),
                action=_parse_action(_value(row, mapping, "action")),
                quantity=quantity,
                unit=canonical_unit,
                currency=_value(row, mapping, "currency").upper(),
                price=price,
            )
            if not transaction.currency:
                raise ValueError("currency is required")
            if not approved:
                pending.append(transaction)
            else:
                accepted.append(transaction)
                accepted_rows.append(row)
            seen.add(transaction_id)
        except (ValueError, InvalidOperation) as error:
            rejected.append(RejectedTransaction(source_row_id, "invalid_transaction", str(error)))
    return TransactionParseResult(tuple(accepted), tuple(pending), tuple(rejected), tuple(accepted_rows))
