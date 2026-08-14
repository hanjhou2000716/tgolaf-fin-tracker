"""Parse the compact, human-friendly transaction form.

The Google Form exposes one free-form transaction description instead of
requiring users to fill the internal accounting columns.  This module keeps
the accepted syntax deliberately small and explicit: ambiguous descriptions
are rejected and sent to the review queue by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re

from transaction_schema import Action


@dataclass(frozen=True)
class CompactTransaction:
    action: Action
    asset_type: str
    symbol: str
    quantity: Decimal
    unit: str
    currency: str
    price: Decimal | None = None


_KNOWN_SYMBOLS = {
    "006208", "6208", "2330", "3665", "403A", "00403A", "00886", "886",
    "00878", "878", "00895", "895", "00685L", "685L", "3455", "8033",
    "QQQM", "QQQ", "SPYG", "VOO", "VTI", "NVDA", "TSM", "TSLA", "AAPL",
}

_ACTION_PATTERNS: tuple[tuple[Action, str], ...] = (
    (Action.SET_BALANCE, r"SET[_ ]?BALANCE|設定餘額|設定現金|對帳|餘額校正|現金校正"),
    (Action.WITHDRAWAL, r"提領|提款|取出|withdraw(?:al)?"),
    (Action.DEPOSIT, r"存入|入金|存款|deposit"),
    (Action.BORROW, r"借款|借入|borrow"),
    (Action.REPAY, r"還款|償還|還借款|repay"),
    (Action.DIVIDEND, r"股息|配息|dividend"),
    (Action.INTEREST, r"利息|interest"),
    (Action.FEE, r"手續費|費用|fee"),
    (Action.TAX, r"交易稅|證交稅|稅|tax"),
    (Action.SELL, r"賣出|賣|sell"),
    (Action.BUY, r"買入|買進|購入|買|buy"),
)

_SYMBOL_RE = re.compile(r"(?<![A-Za-z0-9])(?:[0-9]{4,6}[A-Za-z]?|[A-Za-z]{2,6})(?![A-Za-z0-9])", re.I)
_NUMBER_RE = re.compile(r"(?<![A-Za-z])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_PRICE_RE = re.compile(r"(?:價格|價位|單價|price|at|@)\s*[:：]?\s*\$?([\d,]+(?:\.\d+)?)", re.I)


def _decimal(value: str) -> Decimal:
    try:
        number = Decimal(value.replace(",", ""))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("交易內容中的數字無法解析") from error
    if not number.is_finite() or number < 0:
        raise ValueError("交易數量或金額必須是非負有限數字")
    return number


def _currency(text: str, symbol: str) -> str:
    if re.search(r"USD|美金|美元", text, re.I):
        return "USD"
    if re.search(r"TWD|台幣|新台幣|台元|NT\$|元", text, re.I):
        return "TWD"
    if symbol.upper() in {"QQQM", "QQQ", "SPYG", "VOO", "VTI", "NVDA", "TSM", "TSLA", "AAPL"}:
        return "USD"
    if symbol and (symbol.isdigit() or symbol.upper().endswith("L")):
        return "TWD"
    return ""


def _symbol(text: str, action: Action, currency: str) -> str:
    matches = [m.group(0).upper() for m in _SYMBOL_RE.finditer(text)]
    matches = [m for m in matches if m not in {"TWD", "USD", "NT"}]
    known = [m for m in matches if m in _KNOWN_SYMBOLS]
    if action == Action.SET_BALANCE and currency:
        return currency
    if action in {Action.DEPOSIT, Action.WITHDRAWAL} and currency and not known:
        return currency
    if len(set(known)) > 1:
        raise ValueError("交易內容包含多個標的，請一次只填一筆交易")
    if known:
        return known[0]
    reserved = {
        "BUY", "SELL", "DEPOSIT", "WITHDRAW", "WITHDRAWAL", "BORROW", "REPAY",
        "DIVIDEND", "INTEREST", "FEE", "TAX", "PRICE", "AT",
    }
    candidates = [m for m in matches if m not in reserved]
    if len(set(candidates)) == 1:
        return candidates[0]
    if re.search(r"現金|現鈔|cash", text, re.I):
        if not currency:
            raise ValueError("現金交易請明確填寫 TWD 或 USD")
        return currency
    if action in {Action.BORROW, Action.REPAY} or re.search(r"質押|借款|還款", text):
        return "CURRENT_DEBT"
    if action in {Action.DIVIDEND, Action.INTEREST, Action.FEE, Action.TAX}:
        return currency or ""
    raise ValueError("找不到標的代號，例如 006208、QQQM 或現金")


def _asset_type(symbol: str, text: str) -> str:
    upper = symbol.upper()
    if symbol == "CURRENT_DEBT" or re.search(r"質押|借款|還款", text):
        return "質押負債"
    if symbol in {"TWD", "USD"} or re.search(r"現金|現鈔|cash", text, re.I):
        return "現金_USD" if symbol == "USD" else "現金_TWD"
    if re.search(r"基金|fund", text, re.I):
        return "基金"
    if upper in {"QQQM", "QQQ", "SPYG", "VOO", "VTI", "NVDA", "TSM", "TSLA", "AAPL"}:
        return "美股"
    return "台股"


def parse_compact_transaction(description: str) -> CompactTransaction:
    """Parse a compact transaction description or raise ``ValueError``."""
    # Keep ASCII commas because they can be thousands separators (100,000).
    text = re.sub(r"[，；;]", " ", str(description or "")).strip()
    if not text:
        raise ValueError("交易內容不可空白")

    actions = [action for action, pattern in _ACTION_PATTERNS if re.search(pattern, text, re.I)]
    if len(actions) != 1:
        raise ValueError("請明確填寫一個交易動作，例如買入、賣出、存入或提領")
    action = actions[0]

    provisional_currency = _currency(text, "")
    symbol = _symbol(text, action, provisional_currency)
    currency = _currency(text, symbol)
    if not currency:
        raise ValueError("請填寫幣別，例如 TWD 或 USD")
    if symbol in {"TWD", "USD"} and currency != symbol:
        raise ValueError("現金幣別與標示不一致")

    price_match = _PRICE_RE.search(text)
    price = _decimal(price_match.group(1)) if price_match else None
    quantity_matches = list(_NUMBER_RE.finditer(text))
    if not quantity_matches:
        raise ValueError("找不到交易數量或金額")

    unit_match = re.search(r"(張|股|shares?|lots?|%|TWD|USD|台幣|美元|美金|元)", text, re.I)
    if unit_match:
        raw_unit = unit_match.group(1).lower()
        if raw_unit in {"張", "lot", "lots"}:
            unit = "LOT"
        elif raw_unit in {"股", "share", "shares"}:
            unit = "SHARE"
        elif raw_unit == "%":
            unit = "PERCENT"
        elif raw_unit in {"usd", "美元", "美金"}:
            unit = "USD"
        else:
            unit = "TWD"
    elif action in {Action.DEPOSIT, Action.WITHDRAWAL, Action.DIVIDEND, Action.INTEREST, Action.FEE, Action.TAX, Action.BORROW, Action.REPAY, Action.SET_BALANCE}:
        unit = currency
    else:
        raise ValueError("買賣交易請填寫單位，例如 2 張或 3 股")

    # Prefer the number immediately before the unit.  For cash/financing
    # events there is normally only one number; for trades a second number is
    # allowed only when it is explicitly labelled as a price.
    before_unit = text[: unit_match.start()] if unit_match else text
    candidates = list(_NUMBER_RE.finditer(before_unit))
    if candidates:
        quantity = _decimal(candidates[-1].group(0))
    else:
        quantity = _decimal(quantity_matches[0].group(0))
    if len(quantity_matches) > 1 and price is None and action in {Action.BUY, Action.SELL}:
        raise ValueError("買賣交易有多個數字，請用『價格』或 @ 標示單價")

    if unit == "LOT":
        quantity *= Decimal(1000)
        unit = "SHARE"
    return CompactTransaction(action, _asset_type(symbol, text), symbol, quantity, unit, currency, price)
