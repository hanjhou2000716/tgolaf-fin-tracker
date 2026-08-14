"""Strict Google Form transaction contract and approval gate."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from typing import Sequence
from uuid import UUID, NAMESPACE_URL, uuid5


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
    REVERSAL = "REVERSAL"
    SET_BALANCE = "SET_BALANCE"


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
    reversal_of: str | None = None
    reconciliation_delta: Decimal | None = None
    compatibility_used: str | None = None


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
    "reversal_of": ("reversal_of", "reversal of", "reversal_transaction_id"),
    "target_balance": ("target_balance", "target balance", "目標餘額", "目標現金", "target_amount"),
}

COMPACT_DESCRIPTION_ALIASES = (
    "交易內容",
    "交易内容",
    "交易描述",
    "description",
    "transaction_text",
)


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
    index = mapping.get(field)
    if index is None:
        return ""
    return str(row[index]).strip() if index < len(row) else ""


def _parse_bool(value: str) -> bool:
    normalized = _normalize_header(value)
    if normalized in {"true", "yes", "y", "1", "approved", "核准", "是"}:
        return True
    if normalized in {"false", "no", "n", "0", "pending", "待確認", "否"}:
        return False
    raise ValueError("approved must be an explicit boolean")


def _parse_date(value: str) -> date:
    # Google Sheets localizes timestamps (例如「2026/8/13 下午 3:08:19」).
    # Extract the calendar part before trying strict ISO parsing.
    match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", str(value or ""))
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10].replace("/", "-"))
        except ValueError as error:
            for fmt in ("%m/%d/%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(value.strip(), fmt).date()
                except ValueError:
                    continue
            raise ValueError("transaction_date must be ISO date") from error


def _parse_action(value: str) -> Action:
    normalized = _normalize_header(value).upper()
    # Google Forms choice labels may include both the user-facing synonym and
    # the sign, e.g. "買入 / 存入 (+)".  Resolve the explicit leading action
    # before applying the exact alias table.
    if "買入" in normalized or "買進" in normalized:
        return Action.BUY
    if "賣出" in normalized:
        return Action.SELL
    if "存入" in normalized:
        return Action.DEPOSIT
    if "提領" in normalized:
        return Action.WITHDRAWAL
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
        "REVERSAL": Action.REVERSAL,
        "SET_BALANCE": Action.SET_BALANCE,
        "SETBALANCE": Action.SET_BALANCE,
        "設定餘額": Action.SET_BALANCE,
        "設定現金": Action.SET_BALANCE,
        "對帳": Action.SET_BALANCE,
        "餘額校正": Action.SET_BALANCE,
    }
    if normalized in aliases:
        return aliases[normalized]
    normalized = {"SPINOFF": "SPIN_OFF", "FXCONVERSION": "FX_CONVERSION"}.get(normalized, normalized)
    return Action(normalized)


# Form V2 deliberately keeps the user-facing form small.  The response sheet
# therefore does not contain the internal ledger headers used by the legacy
# fixed-schema form.  This adapter is intentionally separate from the legacy
# parser so historical rows remain byte-for-byte compatible.
V2_HEADERS = {
    "timestamp": ("Timestamp", "時間戳記"),
    "email": ("Email Address", "電子郵件地址", "Email"),
    "transaction_type": ("交易類型", "交易类型", "transaction_type"),
    "target_balance": ("目標餘額", "目標金額", "target_balance"),
    "market": ("市場", "market"),
    "symbol": ("資產代號", "標的代號", "symbol"),
    "quantity": ("數量", "股數", "quantity"),
    "unit": ("單位", "unit"),
    "price": ("價格", "成交價格", "price"),
    "amount": ("金額", "amount"),
    "currency": ("幣別", "貨幣", "currency"),
    "transaction_date": ("交易日期", "日期", "transaction_date"),
    "note": ("備註", "說明", "note"),
}


def _v2_mapping(headers: Sequence[str]) -> dict[str, tuple[int, ...]]:
    """Return every matching column, preserving duplicate Form headers.

    Google Forms repeats labels such as 幣別、交易日期 and 備註 for each
    branched section.  A last-column-wins mapping silently reads an empty
    financing field for a stock row, so the adapter keeps all candidates and
    resolves the first non-empty response per row.
    """
    normalized = {}
    for index, value in enumerate(headers):
        normalized.setdefault(_normalize_header(value), []).append(index)
    mapping: dict[str, tuple[int, ...]] = {}
    for field, aliases in V2_HEADERS.items():
        for alias in aliases:
            indexes = normalized.get(_normalize_header(alias))
            if indexes:
                mapping[field] = tuple(indexes)
                break
    return mapping


def _v2_value(row: Sequence[str], mapping: dict[str, tuple[int, ...]], field: str) -> str:
    for index in mapping.get(field, ()):
        if index < len(row):
            value = str(row[index]).strip()
            if value:
                return value
    return ""


def _v2_decimal(value: str, label: str, *, allow_zero: bool = False) -> Decimal:
    if not value:
        raise ValueError(f"{label} is required")
    try:
        parsed = Decimal(value.replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{label} must be numeric") from error
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise ValueError(f"{label} must be finite and positive")
    return parsed


def _v2_action(value: str) -> Action:
    normalized = _normalize_header(value)
    if normalized in {"買入", "buy"}:
        return Action.BUY
    if normalized in {"賣出", "sell"}:
        return Action.SELL
    if normalized in {"存入", "deposit"}:
        return Action.DEPOSIT
    if normalized in {"提領", "withdrawal", "withdraw"}:
        return Action.WITHDRAWAL
    if normalized in {"借款", "borrow"}:
        return Action.BORROW
    if normalized in {"還款", "repay", "repayment"}:
        return Action.REPAY
    if normalized in {"現金餘額校正", "現金餘額設定", "setbalance", "set_balance"}:
        return Action.SET_BALANCE
    raise ValueError(f"unsupported transaction type: {value}")


def _v2_asset_type(action: Action, market: str, currency: str) -> str:
    if action in {Action.DEPOSIT, Action.WITHDRAWAL, Action.BORROW, Action.REPAY, Action.SET_BALANCE}:
        return "現金_TWD" if currency == "TWD" else "現金_USD"
    market_key = _normalize_header(market)
    if market_key in {"台股", "tw", "taiwan"}:
        return "現貨台股"
    if market_key in {"美股", "us", "usa", "美國"}:
        return "現貨美股"
    raise ValueError("market must be 台股 or 美股")


def _parse_v2_transaction_rows(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    source_sheet: str,
    existing_ids: set[str] | None = None,
) -> TransactionParseResult:
    mapping = _v2_mapping(headers)
    if "transaction_type" not in mapping:
        raise TransactionSchemaError("Form V2 requires 交易類型")
    # Historical response sheets may predate email collection.  New V2 rows
    # still fail closed below when a transaction type is present; legacy rows
    # remain readable so migration does not erase the existing portfolio.
    normalized_list = [_normalize_header(value) for value in headers]

    def first_index(*aliases: str) -> int | None:
        keys = {_normalize_header(alias) for alias in aliases}
        for index, key in enumerate(normalized_list):
            if key in keys:
                return index
        return None

    legacy_asset_index = first_index("資產類別", "asset_type")
    legacy_symbol_index = first_index("資產代號", "asset_type", "symbol")
    legacy_action_index = first_index("交易類型", "action")
    legacy_quantity_index = first_index("數量/股數/金額 (直接填正數即可)", "數量", "quantity")
    legacy_currency_index = first_index("currency", "幣別")
    legacy_unit_index = first_index("unit", "單位")

    def legacy_value(row: Sequence[str], index: int | None) -> str:
        return str(row[index]).strip() if index is not None and index < len(row) else ""

    def legacy_output(asset_type: str, symbol: str, action: Action, quantity: Decimal, transaction_date: date):
        asset = asset_type
        if asset in {"台股", "現貨台股"}:
            asset = "?啗"
        elif asset in {"美股", "現貨美股"}:
            asset = "蝢"
        elif asset.startswith("現金") or asset == "現金_TWD":
            asset = "?暸?_TWD"
        elif asset == "現金_USD":
            asset = "?暸?_USD"
        mode = {
            Action.BUY: "鞎瑕",
            Action.DEPOSIT: "摮",
            Action.BORROW: "摮",
            Action.SELL: "鞈?",
            Action.WITHDRAWAL: "??",
            Action.REPAY: "??",
            Action.SET_BALANCE: "?誨",
        }.get(action, "?誨")
        return (transaction_date.isoformat(), asset, symbol, mode, str(quantity))
    seen = set(existing_ids or set())
    accepted, pending, rejected, accepted_rows = [], [], [], []
    for row_number, raw_row in enumerate(rows, start=2):
        row = tuple(str(value) for value in raw_row)
        source_row_id = f"{source_sheet}:{row_number}"
        transaction_id = str(uuid5(NAMESPACE_URL, source_row_id))
        try:
            if transaction_id in seen:
                raise ValueError("duplicate_transaction_id")
            raw_type = _v2_value(row, mapping, "transaction_type")
            # The response sheet is mixed during migration.  Rows written by
            # the old five-column form have no V2 transaction type.  Keep
            # those rows as a compatibility snapshot, while promoting the
            # exact cash-balance row to a canonical SET_BALANCE event.
            if not raw_type:
                legacy_description = _v2_value(row, mapping, "target_balance")
                numeric_tail = next(
                    (value for value in reversed(row) if re.fullmatch(r"\$?[0-9][0-9,]*(?:\.[0-9]+)?", value.strip())),
                    "",
                )
                if "取代台幣現金金額" in legacy_description and numeric_tail:
                    transaction_date = _parse_date(_v2_value(row, mapping, "timestamp") or "2026-01-01")
                    target = _v2_decimal(numeric_tail, "target balance", allow_zero=True)
                    transaction = Transaction(
                        transaction_id=transaction_id,
                        source_row_id=source_row_id,
                        submitted_at=_v2_value(row, mapping, "timestamp"),
                        submitter_email="legacy@local.invalid",
                        approved=True,
                        transaction_date=transaction_date,
                        asset_type="現金_TWD",
                        symbol="TWD",
                        action=Action.SET_BALANCE,
                        quantity=target,
                        unit="TWD",
                        currency="TWD",
                        compatibility_used="legacy_target_from_price_field",
                    )
                    accepted.append(transaction)
                    accepted_rows.append(legacy_output("現金_TWD", "TWD", Action.SET_BALANCE, target, transaction_date))
                    seen.add(transaction_id)
                    continue
                old_asset = legacy_value(row, legacy_asset_index)
                old_symbol = legacy_value(row, legacy_symbol_index)
                old_action = legacy_value(row, legacy_action_index)
                old_quantity = legacy_value(row, legacy_quantity_index)
                if old_asset and old_action and old_quantity:
                    old_date = _parse_date(_v2_value(row, mapping, "timestamp") or "2026-01-01")
                    old_currency = legacy_value(row, legacy_currency_index).upper() or ("USD" if old_asset == "美股" else "TWD")
                    old_unit = legacy_value(row, legacy_unit_index) or ("股" if "股" in old_quantity else old_currency)
                    old_number = re.sub(r"[^0-9.\-]", "", old_quantity)
                    old_qty = _v2_decimal(old_number, "legacy quantity", allow_zero=True)
                    if "買入" in old_action:
                        old_mode = Action.BUY
                    elif "賣出" in old_action:
                        old_mode = Action.SELL
                    elif "存入" in old_action:
                        old_mode = Action.DEPOSIT
                    elif "提領" in old_action:
                        old_mode = Action.WITHDRAWAL
                    elif "借款" in old_action:
                        old_mode = Action.BORROW
                    elif "還款" in old_action:
                        old_mode = Action.REPAY
                    else:
                        # 「全部取代／覆蓋」 is a legacy snapshot operation.
                        # It is retained only in the compatibility inventory
                        # stream; new ledger rows must use explicit SET_BALANCE
                        # for cash and never infer BUY as a default.
                        old_mode = Action.SET_BALANCE
                    if old_unit in {"張", "撘?"}:
                        old_qty *= Decimal(1000)
                    accepted_rows.append(legacy_output(old_asset, old_symbol or old_currency, old_mode, old_qty, old_date))
                    seen.add(transaction_id)
                    continue
                # Ignore blank migration rows rather than treating them as a
                # malformed new submission.
                if not any(row):
                    continue
                raise ValueError("transaction type is required")
            email = _v2_value(row, mapping, "email")
            if "email" not in mapping:
                raise ValueError("Form V2 requires Email Address")
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                raise ValueError("submitter_email is invalid")
            submitted_at = _v2_value(row, mapping, "timestamp")
            raw_date = _v2_value(row, mapping, "transaction_date") or submitted_at
            transaction_date = _parse_date(raw_date)
            action = _v2_action(raw_type)
            currency = _v2_value(row, mapping, "currency").upper()
            if currency not in {"TWD", "USD"}:
                raise ValueError("currency must be TWD or USD")
            asset_type = _v2_asset_type(action, _v2_value(row, mapping, "market"), currency)
            symbol = _v2_value(row, mapping, "symbol") or currency
            price = None
            if action in {Action.BUY, Action.SELL}:
                quantity, canonical_unit = parse_quantity(
                    _v2_value(row, mapping, "quantity"), _v2_value(row, mapping, "unit")
                )
                price = _v2_decimal(_v2_value(row, mapping, "price"), "price")
            elif action == Action.SET_BALANCE:
                quantity = _v2_decimal(_v2_value(row, mapping, "target_balance"), "target balance", allow_zero=True)
                canonical_unit = currency
                symbol = currency
            else:
                quantity = _v2_decimal(_v2_value(row, mapping, "amount"), "amount")
                canonical_unit = currency
                symbol = currency
            transaction = Transaction(
                transaction_id=transaction_id,
                source_row_id=source_row_id,
                submitted_at=submitted_at,
                submitter_email=email,
                approved=True,
                transaction_date=transaction_date,
                asset_type=asset_type,
                symbol=symbol,
                action=action,
                quantity=quantity,
                unit=canonical_unit,
                currency=currency,
                price=price,
            )
            accepted.append(transaction)
            accepted_rows.append(legacy_output(asset_type, symbol, action, quantity, transaction_date))
            seen.add(transaction_id)
        except (ValueError, InvalidOperation) as error:
            rejected.append(RejectedTransaction(source_row_id, "invalid_transaction", str(error)))
    return TransactionParseResult(tuple(accepted), tuple(pending), tuple(rejected), tuple(accepted_rows))


def parse_transaction_rows(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    source_sheet: str,
    existing_ids: set[str] | None = None,
) -> TransactionParseResult:
    normalized_headers = {_normalize_header(value): index for index, value in enumerate(headers)}
    compact_index = next(
        (normalized_headers.get(_normalize_header(alias)) for alias in COMPACT_DESCRIPTION_ALIASES
         if normalized_headers.get(_normalize_header(alias)) is not None),
        None,
    )
    if compact_index is not None:
        return _parse_compact_transaction_rows(
            headers,
            rows,
            source_sheet=source_sheet,
            existing_ids=existing_ids,
            compact_index=compact_index,
            normalized_headers=normalized_headers,
        )
    # Form V2 uses a compact, user-facing set of questions and therefore does
    # not expose internal UUID/approval columns.  It is checked after the
    # explicit legacy compact marker so mixed historical sheets preserve their
    # old parser path.
    if _normalize_header("交易類型") in normalized_headers:
        return _parse_v2_transaction_rows(
            headers,
            rows,
            source_sheet=source_sheet,
            existing_ids=existing_ids,
        )

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
                reversal_of=_value(row, mapping, "reversal_of") or None,
            )
            if not transaction.currency:
                raise ValueError("currency is required")
            if transaction.action == Action.REVERSAL and not transaction.reversal_of:
                raise ValueError("REVERSAL requires reversal_of")
            if not approved:
                pending.append(transaction)
            else:
                accepted.append(transaction)
                accepted_rows.append(row)
            seen.add(transaction_id)
        except (ValueError, InvalidOperation) as error:
            rejected.append(RejectedTransaction(source_row_id, "invalid_transaction", str(error)))
    return TransactionParseResult(tuple(accepted), tuple(pending), tuple(rejected), tuple(accepted_rows))


def _parse_compact_transaction_rows(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    source_sheet: str,
    existing_ids: set[str] | None,
    compact_index: int,
    normalized_headers: dict[str, int],
) -> TransactionParseResult:
    """Parse the four-field form while producing the canonical ledger object."""
    from compact_transaction import parse_compact_transaction

    def index_for(*aliases: str) -> int | None:
        for alias in aliases:
            index = normalized_headers.get(_normalize_header(alias))
            if index is not None:
                return index
            alias_key = _normalize_header(alias)
            if alias_key:
                for header_key, header_index in normalized_headers.items():
                    if alias_key in header_key:
                        return header_index
        return None

    timestamp_index = index_for("Timestamp", "submitted_at", "提交時間")
    email_index = index_for("Email Address", "email", "submitter_email", "提交者 Email")
    if timestamp_index is None or email_index is None:
        raise TransactionSchemaError("Compact form requires Timestamp and Email Address")

    date_index = index_for("transaction_date", "交易日期", "日期")
    approved_index = index_for("approved", "核准", "審核狀態")
    price_index = index_for("price", "價格", "價格／匯率", "價格/匯率")
    id_index = index_for("transaction_id", "transaction id", "交易編號", "uuid")
    legacy_indices = {
        "asset_type": index_for("asset_type", "asset type", "資產類別"),
        "symbol": index_for("symbol", "資產代號", "標的"),
        "action": index_for("action", "交易類型"),
        "quantity": index_for("quantity", "數量", "數量/股數/金額"),
        "unit": index_for("unit", "單位"),
        "currency": index_for("currency", "幣別"),
    }
    has_legacy_columns = all(index is not None for index in legacy_indices.values())
    seen = set(existing_ids or set())
    accepted, pending, rejected, accepted_rows = [], [], [], []
    mode_by_action = {
        Action.BUY: "買入",
        Action.SELL: "賣出",
        Action.DEPOSIT: "存入",
        Action.WITHDRAWAL: "提領",
        Action.DIVIDEND: "存入",
        Action.INTEREST: "存入",
        Action.FEE: "提領",
        Action.TAX: "提領",
        Action.BORROW: "存入",
        Action.REPAY: "提領",
        Action.SET_BALANCE: "SET_BALANCE",
    }

    for row_number, raw_row in enumerate(rows, start=2):
        row = tuple(str(value) for value in raw_row)
        source_row_id = f"{source_sheet}:{row_number}"
        transaction_id = str(uuid5(NAMESPACE_URL, source_row_id))
        try:
            submitted_at = _value(row, {"submitted_at": timestamp_index}, "submitted_at")
            email = _value(row, {"submitter_email": email_index}, "submitter_email")
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                raise ValueError("submitter_email is invalid")
            if transaction_id in seen:
                raise ValueError("duplicate_transaction_id")
            raw_date = row[date_index].strip() if date_index is not None and date_index < len(row) else ""
            transaction_date = _parse_date(raw_date or submitted_at)
            description = row[compact_index].strip() if compact_index < len(row) else ""

            # Rows created before the compact form was enabled remain in the
            # same response sheet.  Read their old columns through the same
            # generated metadata path so historical holdings are preserved.
            # The original cash snapshot used the description
            # 「取代台幣現金金額」 and stored the target in the legacy price
            # column; keep that exact row compatible without making replace a
            # default for new submissions.
            legacy_snapshot_description = (
                "取代" in description and "現金" in description
            ) or ("對帳" in description and "現金" in description)
            if has_legacy_columns and (not description or legacy_snapshot_description):
                raw_approved = row[approved_index].strip() if approved_index is not None and approved_index < len(row) else ""
                approved = _parse_bool(raw_approved) if raw_approved else True
                raw_legacy_quantity = row[legacy_indices["quantity"]].strip()
                if legacy_snapshot_description and not raw_legacy_quantity:
                    # The original cash snapshot left quantity blank because
                    # its target lived in the legacy price column.
                    legacy_quantity = Decimal("0")
                    legacy_unit = row[legacy_indices["unit"]].strip() or "currency"
                else:
                    legacy_quantity, legacy_unit = parse_quantity(
                        row[legacy_indices["quantity"]], row[legacy_indices["unit"]]
                    )
                legacy_currency = row[legacy_indices["currency"]].strip().upper()
                if not legacy_currency:
                    raise ValueError("currency is required")
                legacy_symbol = row[legacy_indices["symbol"]].strip()
                legacy_asset_type = row[legacy_indices["asset_type"]].strip()
                if not legacy_symbol or not legacy_asset_type:
                    raise ValueError("asset_type and symbol are required")
                raw_legacy_action = row[legacy_indices["action"]].strip()
                normalized_action = _normalize_header(raw_legacy_action)
                legacy_replace_action = normalized_action in {"取代", "覆蓋", "更新", "replace", "overwrite"}
                legacy_action = _parse_action(raw_legacy_action) if not (legacy_replace_action or legacy_snapshot_description) else Action.SET_BALANCE
                # The pre-V2 form used 取代/覆蓋 plus the old price field for
                # a cash-balance snapshot.  Adapt only an explicit cash row;
                # all other unknown actions remain rejected.
                if (legacy_replace_action or legacy_snapshot_description) and (
                    legacy_asset_type.lower().startswith(("現金", "cash")) or legacy_symbol.upper() in {"TWD", "USD"}
                ):
                    legacy_action = Action.SET_BALANCE
                    legacy_unit = legacy_currency
                    if price_index is not None and price_index < len(row) and row[price_index].strip():
                        legacy_quantity = Decimal(row[price_index].replace(",", "").replace("$", "").strip())
                        if not legacy_quantity.is_finite() or legacy_quantity < 0:
                            raise ValueError("SET_BALANCE target must be finite and non-negative")
                transaction = Transaction(
                    transaction_id=(row[id_index].strip() if id_index is not None and id_index < len(row) and row[id_index].strip() else transaction_id),
                    source_row_id=source_row_id,
                    submitted_at=submitted_at,
                    submitter_email=email,
                    approved=approved,
                    transaction_date=transaction_date,
                    asset_type=legacy_asset_type,
                    symbol=legacy_symbol,
                    action=legacy_action,
                    quantity=legacy_quantity,
                    unit=legacy_unit,
                    currency=legacy_currency,
                    compatibility_used=("legacy_target_from_price_field" if legacy_action == Action.SET_BALANCE and price_index is not None and price_index < len(row) and row[price_index].strip() else None),
                )
                seen.add(transaction.transaction_id)
                if not approved:
                    pending.append(transaction)
                    continue
                accepted.append(transaction)
                accepted_rows.append((
                    transaction_date.isoformat(), legacy_asset_type, legacy_symbol,
                    mode_by_action[legacy_action], str(legacy_quantity),
                ))
                continue

            approved = True
            if approved_index is not None:
                raw_approved = row[approved_index].strip() if approved_index < len(row) else ""
                if raw_approved:
                    approved = _parse_bool(raw_approved)
            compact = parse_compact_transaction(description)
            price_text = row[price_index].strip() if price_index is not None and price_index < len(row) else ""
            price = Decimal(price_text.replace(",", "").replace("$", "")) if price_text else compact.price
            if price is not None and (not price.is_finite() or price < 0):
                raise ValueError("price must be finite and non-negative")
            transaction = Transaction(
                transaction_id=transaction_id,
                source_row_id=source_row_id,
                submitted_at=submitted_at,
                submitter_email=email,
                approved=approved,
                transaction_date=transaction_date,
                asset_type=compact.asset_type,
                symbol=compact.symbol,
                action=compact.action,
                quantity=compact.quantity,
                unit=compact.unit,
                currency=compact.currency,
                price=price,
            )
            seen.add(transaction_id)
            if not approved:
                pending.append(transaction)
                continue
            accepted.append(transaction)
            # The legacy inventory adapter consumes a compact canonical row.
            legacy_asset_type = compact.asset_type
            legacy_symbol = compact.symbol
            if compact.action in {Action.DIVIDEND, Action.INTEREST, Action.FEE, Action.TAX}:
                legacy_asset_type = "現金_USD" if compact.currency == "USD" else "現金_TWD"
                legacy_symbol = compact.currency
            accepted_rows.append((
                transaction_date.isoformat(),
                legacy_asset_type,
                legacy_symbol,
                mode_by_action[compact.action],
                str(compact.quantity),
            ))
        except (ValueError, InvalidOperation) as error:
            rejected.append(RejectedTransaction(source_row_id, "invalid_transaction", str(error)))
    return TransactionParseResult(tuple(accepted), tuple(pending), tuple(rejected), tuple(accepted_rows))
