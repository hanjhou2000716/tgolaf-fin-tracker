"""Strict Google Form transaction contract and approval gate."""

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
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
    SET_PLEDGE_RATE = "SET_PLEDGE_RATE"


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

# The response sheet is an input transport, not the domain contract.  These
# names are the only schema versions accepted at the ingestion boundary.
CURRENT_FORM_SCHEMA = "CURRENT"
FORM_V2_SCHEMA = "FORM_V2"
LEGACY_SCHEMA = "LEGACY"
LEGACY_COMPACT_SCHEMA = "LEGACY_COMPACT"
UNKNOWN_SCHEMA = "UNKNOWN"

# Headers emitted by Google Forms are transport metadata, not accounting
# fields.  Keep this list deliberately small: unknown headers which look like
# transaction data remain fail-closed, while harmless response metadata can be
# added without breaking ingestion.
NON_TRANSACTION_HEADER_HINTS = (
    "responseid",
    "rownumber",
    "序號",
    "編號",
    "回覆編號",
    "回覆時間",
    "提交者姓名",
    "name",
    "notes",
    "備註",
    "comment",
)

SIMPLE_SCHEMA_ALIASES = {
    "timestamp": ("Timestamp", "提交時間"),
    "email": ("Email Address", "Email", "提交者 Email"),
    "transaction_type": ("交易類型", "交易類型（標的＋動作）", "simple_transaction_type"),
    "unit": ("交易單位", "transaction_unit"),
    "quantity": ("交易數量", "transaction_quantity"),
}


def _has_any_normalized(normalized: set[str], aliases: Sequence[str]) -> bool:
    return any(_normalize_header(alias) in normalized for alias in aliases)


def _schema_header_candidates(schema: str | None = None) -> dict[str, tuple[str, ...]]:
    """Return schema-specific aliases without importing simple_transaction."""
    if schema == CURRENT_FORM_SCHEMA:
        aliases = dict(SIMPLE_SCHEMA_ALIASES)
        # Keep optional legacy metadata recognized as harmless extras, while
        # giving the current Form's labels their own canonical names.
        aliases.update({field: values for field, values in HEADER_ALIASES.items() if field not in aliases})
        return aliases
    if schema == FORM_V2_SCHEMA:
        return dict(V2_HEADERS)
    aliases = dict(HEADER_ALIASES)
    aliases["description"] = COMPACT_DESCRIPTION_ALIASES
    return aliases


def _schema_field_for_header(header: str, schema: str | None = None) -> str | None:
    normalized = _normalize_header(header)
    for field, aliases in _schema_header_candidates(schema).items():
        if normalized in {_normalize_header(alias) for alias in aliases}:
            return field
    return None


def _schema_required_fields(schema: str, headers: Sequence[str]) -> tuple[str, ...]:
    # A partially edited three-question Form is identified as CURRENT by
    # detect_schema when all five fields exist; otherwise UNKNOWN remains
    # fail-closed and reports the legacy required-field set.
    if schema == CURRENT_FORM_SCHEMA:
        return ("timestamp", "email", "transaction_type", "unit", "quantity")
    if schema == FORM_V2_SCHEMA:
        return ("timestamp", "email", "transaction_type", "quantity", "unit")
    if schema == LEGACY_COMPACT_SCHEMA:
        return ("timestamp", "email", "description")
    return REQUIRED_FIELDS


def _duplicate_headers_are_row_disjoint(fields: dict[str, list[int]], required: Sequence[str], rows: Sequence[Sequence[str]]) -> bool:
    """Accept a known mixed response sheet only when duplicate columns are unambiguous.

    Google Forms keeps old questions in a response sheet after a form is
    edited.  A duplicated required header is safe when each row populates at
    most one candidate for that field; a row that fills two candidates remains
    quarantined because there is no deterministic accounting source.
    """
    duplicate_required = [field for field in required if len(fields.get(field, [])) > 1]
    if not duplicate_required or not rows:
        return False
    for row in rows:
        for field in duplicate_required:
            populated = [
                index for index in fields[field]
                if index < len(row) and str(row[index]).strip()
            ]
            if len(populated) > 1:
                return False
    return True


def analyze_schema(
    headers: Sequence[str],
    *,
    schema: str | None = None,
    row_count: int = 0,
    rows: Sequence[Sequence[str]] | None = None,
) -> dict:
    """Create a private, non-financial schema diagnostic.

    Header order is intentionally excluded from the fingerprint.  A reordered
    Google response sheet is therefore safe, while missing/ambiguous fields
    and unknown accounting-looking headers remain fail-closed.
    """
    header_values = [str(value or "").strip() for value in headers]
    detected = schema or detect_schema(header_values)
    required = _schema_required_fields(detected, header_values)
    fields: dict[str, list[int]] = {}
    empty_headers = 0
    unknown_headers: list[str] = []
    ignored_headers: list[str] = []
    for index, header in enumerate(header_values):
        if not header:
            empty_headers += 1
            continue
        field = _schema_field_for_header(header, detected)
        if field:
            fields.setdefault(field, []).append(index)
            continue
        normalized = _normalize_header(header)
        if any(hint in normalized for hint in NON_TRANSACTION_HEADER_HINTS):
            ignored_headers.append(header)
        else:
            unknown_headers.append(header)
    missing = [field for field in required if field not in fields]
    duplicate_fields = {
        field: indexes for field, indexes in fields.items() if len(indexes) > 1
    }
    duplicate_required = {field: indexes for field, indexes in duplicate_fields.items() if field in required}
    accounting_unknown = [
        header for header in unknown_headers
        if any(token in _normalize_header(header) for token in (
            "交易", "資產", "標的", "代號", "數量", "單位", "幣別", "價格", "金額",
            "action", "symbol", "quantity", "unit", "currency", "price", "amount",
        ))
    ]
    duplicate_resolved = _duplicate_headers_are_row_disjoint(fields, required, rows or ())
    safe = not missing and (not duplicate_required or duplicate_resolved) and not accounting_unknown and detected != UNKNOWN_SCHEMA
    canonical_mapping = {
        field: indexes[0] for field, indexes in sorted(fields.items()) if indexes
    }
    fingerprint_payload = {
        "schema": detected,
        "required": list(required),
        "mapping": sorted(canonical_mapping),
        "missing": sorted(missing),
        "duplicate": {key: len(value) for key, value in sorted(duplicate_fields.items())},
        "unknown": sorted(_normalize_header(value) for value in accounting_unknown),
        "ignored": sorted(_normalize_header(value) for value in ignored_headers),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return {
        "schema": detected,
        "rowCount": max(0, int(row_count or 0)),
        "fingerprint": fingerprint,
        "requiredFields": list(required),
        "canonicalMapping": canonical_mapping,
        "missingFields": sorted(missing),
        "duplicateFields": {key: len(value) for key, value in sorted(duplicate_fields.items())},
        "duplicateResolved": duplicate_resolved,
        "unknownHeaders": sorted(accounting_unknown),
        "ignoredExtraHeaders": sorted(ignored_headers),
        "safe": safe,
        "reason": None if safe else (
            "missing_required_fields" if missing else
            "duplicate_required_fields" if duplicate_required else
            "unknown_accounting_headers" if accounting_unknown else
            "unknown_schema"
        ),
    }


def schema_drift_digest(diagnostics: Sequence[dict]) -> str:
    """Digest only schema shape, never row values or financial payloads."""
    unsafe = [
        {
            "sheet": str(item.get("sheet") or ""),
            "schema": item.get("schema"),
            "fingerprint": item.get("fingerprint"),
            "reason": item.get("reason"),
            "missing": item.get("missingFields", []),
            "duplicate": item.get("duplicateFields", {}),
            "unknown": item.get("unknownHeaders", []),
        }
        for item in diagnostics
        if not item.get("safe", True)
    ]
    if not unsafe:
        return ""
    encoded = json.dumps(sorted(unsafe, key=lambda item: (item["sheet"], item["fingerprint"])), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def detect_schema(headers: Sequence[str]) -> str:
    """Detect a known response shape without inspecting row values.

    Unknown headers intentionally return ``UNKNOWN``.  They must never be
    routed into the historical heuristic inventory reducer.
    """
    normalized = {_normalize_header(value) for value in headers}
    # Import lazily to avoid simple_transaction -> transaction_schema cycles.
    from simple_transaction import is_simple_form_headers
    if is_simple_form_headers(headers):
        return CURRENT_FORM_SCHEMA
    if _has_any_normalized(normalized, COMPACT_DESCRIPTION_ALIASES):
        return LEGACY_COMPACT_SCHEMA
    if _normalize_header("交易類型") in normalized or _has_any_normalized(
        normalized, ("market", "市場")
    ) and _has_any_normalized(normalized, ("symbol", "資產代號")):
        return FORM_V2_SCHEMA
    try:
        resolve_headers(headers)
        return LEGACY_SCHEMA
    except TransactionSchemaError:
        return UNKNOWN_SCHEMA


def adapt_known_legacy_rows(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> tuple[tuple[str, ...], ...]:
    """Migrate only a recognized legacy inventory shape to five canonical cells.

    This is deliberately header-mapped.  It is not a fallback that scans an
    arbitrary row for numbers or keywords.
    """
    normalized = {_normalize_header(value): index for index, value in enumerate(headers)}

    def index_for(*aliases: str) -> int | None:
        for alias in aliases:
            index = normalized.get(_normalize_header(alias))
            if index is not None:
                return index
        return None

    indexes = {
        "date": index_for("Timestamp", "時間戳記", "transaction_date", "交易日期", "日期"),
        "asset": index_for("asset_type", "資產類別", "資產類型"),
        "symbol": index_for("symbol", "資產代號", "股票代號", "代號"),
        "action": index_for("action", "交易類型", "異動類型", "操作"),
        "quantity": index_for("quantity", "數量", "數量/股數/金額 (直接填正數即可)", "金額"),
    }
    if any(index is None for index in indexes.values()):
        raise TransactionSchemaError("Known legacy schema is missing migration columns")

    migrated = []
    for row in rows:
        values = tuple(str(row[indexes[field]]).strip() if indexes[field] < len(row) else "" for field in ("date", "asset", "symbol", "action", "quantity"))
        if any(values[1:]):
            migrated.append(values)
    return tuple(migrated)


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
        "利率": Action.SET_PLEDGE_RATE,
        "設定利率": Action.SET_PLEDGE_RATE,
        "SET_PLEDGE_RATE": Action.SET_PLEDGE_RATE,
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
    if normalized in {"利率", "設定利率", "setpledgerate", "set_pledge_rate"}:
        return Action.SET_PLEDGE_RATE
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
    mixed_legacy_shape = len(headers) > len(V2_HEADERS) and {
        "timestamp", "market", "symbol", "quantity", "unit", "currency",
    }.issubset(mapping)
    if "transaction_type" not in mapping and not mixed_legacy_shape:
        raise TransactionSchemaError("Form V2 requires 交易類型")
    # Historical response sheets may predate email collection.  New V2 rows
    # still fail closed below when a transaction type is present; legacy rows
    # remain readable so migration does not erase the existing portfolio.
    normalized_list = [_normalize_header(value) for value in headers]

    def first_index(*aliases: str) -> int | None:
        # Prefer the explicit legacy header when a mixed sheet also contains
        # the new ``交易類型`` question.  A first-column-wins lookup would
        # mistake the simple question for the legacy action column.
        for alias in aliases:
            key = _normalize_header(alias)
            for index, header_key in enumerate(normalized_list):
                if header_key == key:
                    return index
        return None

    legacy_asset_index = first_index("asset_type", "資產類別")
    legacy_symbol_index = first_index("symbol", "資產代號", "asset_type")
    legacy_action_index = first_index("action", "交易類型")
    legacy_quantity_index = first_index("quantity", "數量/股數/金額 (直接填正數即可)", "數量")
    legacy_currency_index = first_index("currency", "幣別")
    legacy_unit_index = first_index("unit", "單位")

    if legacy_symbol_index == legacy_asset_index:
        # Mixed sheets can expose ``asset_type`` before the separate symbol
        # column; never let the first duplicate consume the market label.
        symbol_candidate = first_index("symbol")
        if symbol_candidate is not None:
            legacy_symbol_index = symbol_candidate

    def legacy_value(row: Sequence[str], index: int | None) -> str:
        return str(row[index]).strip() if index is not None and index < len(row) else ""

    def has_v2_value(row: Sequence[str], field: str, *, exclude: set[int] | None = None) -> bool:
        """Return whether a V2 candidate column contains a response.

        Google Forms can leave the legacy five-column branch and the V2 branch
        side-by-side in one response sheet.  In that mixed layout the first
        ``交易類型`` column belongs to the legacy branch, so simply taking the
        first non-empty duplicate makes every old BUY/SELL row look like a
        malformed V2 row.  Keep the candidate indexes explicit and ignore the
        known legacy index when deciding which branch produced the row.
        """
        excluded = exclude or set()
        return any(
            index not in excluded and index < len(row) and str(row[index]).strip()
            for index in mapping.get(field, ())
        )

    def legacy_output(asset_type: str, symbol: str, action: Action, quantity: Decimal, transaction_date: date, *, signed: bool = False):
        asset = asset_type
        if asset in {"台股", "現貨台股"}:
            asset = "台股"
        elif asset in {"美股", "現貨美股"}:
            asset = "美股"
        elif asset.startswith("現金") or asset == "現金_TWD":
            asset = "現金_TWD"
        elif asset == "現金_USD":
            asset = "現金_USD"
        mode = {
            Action.BUY: "買入",
            Action.DEPOSIT: "存入",
            Action.BORROW: "存入",
            Action.SELL: "賣出",
            Action.WITHDRAWAL: "提領",
        Action.REPAY: "提領",
        Action.SET_BALANCE: "取代",
        Action.SET_PLEDGE_RATE: "取代",
        }.get(action, "取代")
        if signed and action in {Action.BUY, Action.DEPOSIT, Action.BORROW}:
            mode = f"{mode} (+)"
        elif signed and action in {Action.SELL, Action.WITHDRAWAL, Action.REPAY}:
            mode = f"{mode} (-)"
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
            legacy_asset = legacy_value(row, legacy_asset_index)
            legacy_action = legacy_value(row, legacy_action_index)
            legacy_quantity = legacy_value(row, legacy_quantity_index)
            legacy_row = bool(legacy_asset and legacy_action and legacy_quantity)
            # The actual Form_Responses3 layout has old columns A–G followed
            # by the V2 branch.  The V2 transaction type is the branch
            # discriminator: a legacy cash row may legitimately use the V2
            # target-balance column for its old description, so other V2
            # fields must not force it down the strict path.
            legacy_type_indexes = {legacy_action_index} if legacy_action_index is not None else set()
            has_v2_transaction_type = has_v2_value(row, "transaction_type", exclude=legacy_type_indexes)
            raw_type = "" if legacy_row and not has_v2_transaction_type else _v2_value(row, mapping, "transaction_type")
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
                # Some exported historical fixtures contain UTF-8 mojibake;
                # normalize the common BUY/SELL labels before action routing.
                if old_action.startswith(chr(0x00e8) + chr(0x00b2) + chr(0x00b7)) or old_action.startswith(chr(0x978e) + chr(0x7455)):
                    old_action = "鞎瑕"
                elif old_action.startswith("\u00e8\u00b3\u00a3"):
                    old_action = "鞈?"
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
                    # Override mojibake labels after the compatibility table;
                    # the table's legacy Chinese literals vary by export.
                    if old_action.startswith(chr(0x978e) + chr(0x7455)):
                        old_mode = Action.BUY
                    elif old_action.startswith(chr(0x8ce3) + chr(0x6b3e)):
                        old_mode = Action.SELL
                    if old_unit in {"張", "撘?"}:
                        old_qty *= Decimal(1000)
                    normalized_asset = old_asset
                    if old_mode in {Action.BUY, Action.SELL}:
                        try:
                            normalized_asset = _v2_asset_type(old_mode, old_asset, old_currency)
                        except ValueError:
                            # Keep an unknown historical market label auditable;
                            # it must not make the entire legacy row disappear.
                            normalized_asset = old_asset
                    normalized_unit = "SHARE" if old_unit.upper() in {"LOT", "LOTS"} or old_unit in {"張", "撘?"} else (old_unit or old_currency)
                    accepted.append(Transaction(
                        transaction_id=transaction_id,
                        source_row_id=source_row_id,
                        submitted_at=_v2_value(row, mapping, "timestamp"),
                        submitter_email="legacy@local.invalid",
                        approved=True,
                        transaction_date=old_date,
                        asset_type=normalized_asset,
                        symbol=old_symbol or old_currency,
                        action=old_mode,
                        quantity=old_qty,
                        unit=normalized_unit,
                        currency=old_currency,
                        compatibility_used="legacy_mixed_form_row",
                    ))
                    accepted_rows.append(legacy_output(old_asset, old_symbol or old_currency, old_mode, old_qty, old_date, signed=True))
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
    # A Google Form response sheet can retain the previous branch columns
    # after the form is simplified.  When the new three-question headers and
    # legacy/V2 headers coexist, parse each row through the branch it actually
    # populated.  Sending the whole sheet to the simple parser makes every
    # historical row look malformed because its three new cells are empty.
    from simple_transaction import is_simple_form_headers, parse_simple_transaction_rows
    simple_shape = is_simple_form_headers(headers)
    if simple_shape and _has_mixed_form_columns(headers):
        return _parse_mixed_form_rows(
            headers,
            rows,
            source_sheet=source_sheet,
            existing_ids=existing_ids,
        )
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
    # The current public form is a single page with three user-entered
    # questions.  Its explicit 交易單位／交易數量 headers distinguish it from
    # the older Form V2 branch, which uses generic 單位／數量 headers.
    if simple_shape:
        return parse_simple_transaction_rows(
            headers,
            rows,
            source_sheet=source_sheet,
            existing_ids=existing_ids,
        )
    # Form V2 uses a compact, user-facing set of questions and therefore does
    # not expose internal UUID/approval columns.  It is checked after the
    # explicit legacy compact marker so mixed historical sheets preserve their
    # old parser path.
    v2_mapping = _v2_mapping(headers)
    mixed_legacy_shape = len(headers) > len(V2_HEADERS) and {
        "timestamp", "market", "symbol", "quantity", "unit", "currency",
    }.issubset(v2_mapping)
    if mixed_legacy_shape:
        return _parse_v2_transaction_rows(
            headers,
            rows,
            source_sheet=source_sheet,
            existing_ids=existing_ids,
        )
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


def _has_mixed_form_columns(headers: Sequence[str]) -> bool:
    """Return true when current three-question and historical branch columns coexist."""
    simple_headers = {
        _normalize_header(alias)
        for aliases in SIMPLE_SCHEMA_ALIASES.values()
        for alias in aliases
    }
    legacy_branch_headers = {
        "asset_type", "symbol", "action", "market", "資產類別", "資產代號",
        "市場", "價格", "成交價格", "price", "金額", "amount", "幣別",
        "currency", "交易日期", "日期", "目標餘額", "target_balance", "數量",
        "股數", "單位", "unit", "備註", "note", "approved", "核准",
    }
    normalized = {_normalize_header(value) for value in headers}
    return bool(normalized - simple_headers) and bool(normalized & {
        _normalize_header(value) for value in legacy_branch_headers
    })


def _rebase_mixed_result(
    result: TransactionParseResult,
    *,
    source_sheet: str,
    row_number: int,
    seen: set[str],
) -> TransactionParseResult:
    """Restore the real source row ID after parsing a one-row branch slice."""
    source_row_id = f"{source_sheet}:{row_number}"
    transaction_id = str(uuid5(NAMESPACE_URL, source_row_id))
    accepted: list[Transaction] = []
    pending: list[Transaction] = []
    rejected = list(result.rejected)
    if transaction_id in seen and (result.accepted or result.pending):
        rejected.append(RejectedTransaction(source_row_id, "duplicate_transaction_id", "duplicate_transaction_id"))
        return TransactionParseResult((), (), tuple(rejected), result.accepted_rows)
    for item in result.accepted:
        accepted.append(replace(item, transaction_id=transaction_id, source_row_id=source_row_id))
    for item in result.pending:
        pending.append(replace(item, transaction_id=transaction_id, source_row_id=source_row_id))
    if accepted or pending:
        seen.add(transaction_id)
    rejected = [replace(item, source_row_id=source_row_id) for item in rejected]
    return TransactionParseResult(tuple(accepted), tuple(pending), tuple(rejected), result.accepted_rows)


def _parse_mixed_form_rows(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    source_sheet: str,
    existing_ids: set[str] | None = None,
) -> TransactionParseResult:
    """Parse a mixed response sheet without guessing across branch columns."""
    from simple_transaction import parse_simple_transaction_rows

    normalized_headers = [_normalize_header(value) for value in headers]

    def indexes_for(field: str) -> tuple[int, ...]:
        aliases = SIMPLE_SCHEMA_ALIASES[field]
        normalized = {_normalize_header(alias) for alias in aliases}
        return tuple(index for index, header in enumerate(normalized_headers) if header in normalized)

    type_indexes = indexes_for("transaction_type")
    unit_indexes = indexes_for("unit")
    quantity_indexes = indexes_for("quantity")
    seen = set(existing_ids or set())
    accepted: list[Transaction] = []
    pending: list[Transaction] = []
    rejected: list[RejectedTransaction] = []
    accepted_rows: list[tuple[str, ...]] = []
    for row_number, raw_row in enumerate(rows, start=2):
        row = tuple(str(value) for value in raw_row)
        type_values = [
            str(row[index]).strip()
            for index in type_indexes
            if index < len(row) and str(row[index]).strip()
        ]
        unit_values = [
            str(row[index]).strip()
            for index in unit_indexes
            if index < len(row) and str(row[index]).strip()
        ]
        quantity_values = [
            str(row[index]).strip()
            for index in quantity_indexes
            if index < len(row) and str(row[index]).strip()
        ]
        # A row with any of the explicit new-question cells belongs to the
        # simple branch; old rows leave those cells empty and use the V2/legacy
        # adapter below.  This keeps invalid new rows rejected rather than
        # accidentally interpreted as an old transaction.
        use_simple = bool(type_values) and bool(unit_values or quantity_values)
        if use_simple:
            result = parse_simple_transaction_rows(
                headers, [row], source_sheet=source_sheet, existing_ids=set()
            )
        else:
            result = _parse_v2_transaction_rows(
                headers, [row], source_sheet=source_sheet, existing_ids=set()
            )
        rebased = _rebase_mixed_result(
            result, source_sheet=source_sheet, row_number=row_number, seen=seen
        )
        accepted.extend(rebased.accepted)
        pending.extend(rebased.pending)
        rejected.extend(rebased.rejected)
        accepted_rows.extend(rebased.accepted_rows)
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
        Action.SET_PLEDGE_RATE: "取代",
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
