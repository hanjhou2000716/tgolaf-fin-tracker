"""Formal portfolio transaction ledger and deterministic state reducer.

The Google Form parser validates the shape of a transaction.  This module is
the next layer: it applies the validated event to cash, positions and
financing balances.  Events are immutable and keyed by ``transaction_id``;
replaying an identical event is a no-op while reusing an ID with different
content fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from ledger import transaction_payload
from transaction_schema import Action, Transaction


class PortfolioLedgerError(ValueError):
    """Raised when an event would make the account state inconsistent."""


class PortfolioLedgerConflict(PortfolioLedgerError):
    """Raised when an immutable transaction UUID is reused with new content."""


@dataclass
class PortfolioState:
    """Account balances derived solely from accepted transaction events."""

    positions: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    cash: dict[str, Decimal] = field(default_factory=dict)
    debt: dict[str, Decimal] = field(default_factory=dict)
    dividends: Decimal = Decimal("0")
    interest: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    taxes: Decimal = Decimal("0")

    def position(self, asset_type: str, symbol: str) -> Decimal:
        return self.positions.get((asset_type, symbol), Decimal("0"))

    def cash_balance(self, currency: str) -> Decimal:
        return self.cash.get(currency.upper(), Decimal("0"))

    def debt_balance(self, currency: str) -> Decimal:
        return self.debt.get(currency.upper(), Decimal("0"))

    def as_dict(self) -> dict:
        return {
            "positions": {
                f"{asset_type}:{symbol}": str(quantity)
                for (asset_type, symbol), quantity in sorted(self.positions.items())
                if quantity
            },
            "cash": {currency: str(value) for currency, value in sorted(self.cash.items()) if value},
            "debt": {currency: str(value) for currency, value in sorted(self.debt.items()) if value},
            "dividends": str(self.dividends),
            "interest": str(self.interest),
            "fees": str(self.fees),
            "taxes": str(self.taxes),
        }


def _positive(value: Decimal, label: str) -> Decimal:
    if not value.is_finite() or value <= 0:
        raise PortfolioLedgerError(f"{label} must be positive")
    return value


def _currency(tx: Transaction) -> str:
    currency = tx.currency.upper().strip()
    if currency not in {"TWD", "USD"}:
        raise PortfolioLedgerError(f"unsupported currency: {tx.currency}")
    return currency


def _position_key(tx: Transaction) -> tuple[str, str]:
    if not tx.asset_type.strip() or not tx.symbol.strip():
        raise PortfolioLedgerError("asset_type and symbol are required for position events")
    return tx.asset_type.strip(), tx.symbol.strip()


def _change_cash(state: PortfolioState, currency: str, delta: Decimal, *, allow_negative: bool = False) -> None:
    current = state.cash_balance(currency)
    updated = current + delta
    if not allow_negative and updated < 0:
        raise PortfolioLedgerError(f"insufficient {currency} cash")
    state.cash[currency] = updated


def _change_position(state: PortfolioState, key: tuple[str, str], delta: Decimal) -> None:
    current = state.positions.get(key, Decimal("0"))
    updated = current + delta
    if updated < 0:
        raise PortfolioLedgerError(f"insufficient position: {key[0]} {key[1]}")
    if updated:
        state.positions[key] = updated
    else:
        state.positions.pop(key, None)


def _change_debt(state: PortfolioState, currency: str, delta: Decimal) -> None:
    current = state.debt_balance(currency)
    updated = current + delta
    if updated < 0:
        raise PortfolioLedgerError(f"repayment exceeds {currency} debt")
    if updated:
        state.debt[currency] = updated
    else:
        state.debt.pop(currency, None)


def _notional(tx: Transaction) -> Decimal:
    price = tx.price
    if price is None:
        raise PortfolioLedgerError(f"{tx.action.value} requires price")
    return _positive(tx.quantity, "quantity") * _positive(price, "price")


def _transfer_symbol(symbol: str) -> tuple[str, str]:
    parts = [part.strip() for part in symbol.split("->")]
    if len(parts) != 2 or not all(parts):
        raise PortfolioLedgerError("TRANSFER symbol must be SOURCE->TARGET")
    return parts[0], parts[1]


def _fx_symbol(symbol: str) -> tuple[str, str]:
    parts = [part.strip().upper() for part in symbol.split("/")]
    if len(parts) != 2 or not all(parts):
        raise PortfolioLedgerError("FX_CONVERSION symbol must be SOURCE/TARGET")
    return parts[0], parts[1]


def _apply_one(state: PortfolioState, tx: Transaction, *, inverse: bool = False) -> None:
    """Apply one event. ``inverse`` is used only for REVERSAL events."""

    original_action = tx.action
    action = original_action
    if inverse:
        inverse_actions = {
            Action.BUY: Action.SELL,
            Action.SELL: Action.BUY,
            Action.DEPOSIT: Action.WITHDRAWAL,
            Action.WITHDRAWAL: Action.DEPOSIT,
            Action.BORROW: Action.REPAY,
            Action.REPAY: Action.BORROW,
            Action.SPLIT: Action.SPLIT,
            Action.SPIN_OFF: Action.SPIN_OFF,
            Action.TRANSFER: Action.TRANSFER,
            Action.FX_CONVERSION: Action.FX_CONVERSION,
        }
        if action not in inverse_actions:
            raise PortfolioLedgerError(f"cannot reverse action: {action.value}")
        action = inverse_actions[action]

    if action in {Action.BUY, Action.SELL}:
        key = _position_key(tx)
        notional = _notional(tx)
        if action == Action.BUY:
            _change_position(state, key, tx.quantity)
            _change_cash(state, _currency(tx), -notional)
        else:
            _change_position(state, key, -tx.quantity)
            _change_cash(state, _currency(tx), notional)
        return

    if action in {Action.DEPOSIT, Action.WITHDRAWAL, Action.DIVIDEND, Action.INTEREST, Action.FEE, Action.TAX}:
        amount = _positive(tx.quantity, "quantity")
        sign = -1 if action in {Action.WITHDRAWAL, Action.INTEREST, Action.FEE, Action.TAX} else 1
        if inverse and original_action in {Action.DIVIDEND, Action.INTEREST, Action.FEE, Action.TAX}:
            sign *= -1
        _change_cash(state, _currency(tx), amount * sign, allow_negative=action in {Action.INTEREST, Action.FEE, Action.TAX})
        if action == Action.DIVIDEND:
            state.dividends += amount * (-1 if inverse else 1)
        elif action == Action.INTEREST:
            state.interest += amount * (-1 if inverse else 1)
        elif action == Action.FEE:
            state.fees += amount * (-1 if inverse else 1)
        elif action == Action.TAX:
            state.taxes += amount * (-1 if inverse else 1)
        return

    if action in {Action.BORROW, Action.REPAY}:
        amount = _positive(tx.quantity, "quantity")
        currency = _currency(tx)
        if action == Action.BORROW:
            _change_debt(state, currency, amount)
            _change_cash(state, currency, amount)
        else:
            _change_debt(state, currency, -amount)
            _change_cash(state, currency, -amount)
        return

    if action == Action.SPLIT:
        key = _position_key(tx)
        ratio = _positive(tx.quantity, "split ratio")
        if inverse:
            ratio = Decimal("1") / ratio
        state.positions[key] = state.position(*key) * ratio
        if not state.positions[key]:
            state.positions.pop(key, None)
        return

    if action in {Action.SPIN_OFF, Action.TRANSFER}:
        source, target = _transfer_symbol(tx.symbol)
        key_source = (tx.asset_type.strip(), source)
        key_target = (tx.asset_type.strip(), target)
        amount = _positive(tx.quantity, "quantity")
        if action == Action.TRANSFER:
            _change_position(state, key_source if not inverse else key_target, -amount)
            _change_position(state, key_target if not inverse else key_source, amount)
        else:
            # Spin-offs create a new holding; the parent remains unchanged.
            _change_position(state, key_target if not inverse else key_source, amount)
        return

    if action == Action.FX_CONVERSION:
        source, target = _fx_symbol(tx.symbol)
        amount = _positive(tx.quantity, "quantity")
        rate = _positive(tx.price or Decimal("0"), "FX rate")
        if inverse:
            source, target = target, source
            amount = amount * rate
            rate = Decimal("1") / rate
        _change_cash(state, source, -amount)
        _change_cash(state, target, amount * rate)
        return

    raise PortfolioLedgerError(f"unsupported action: {tx.action.value}")


class PortfolioLedger:
    """Idempotent append-only ledger that can rebuild state from events."""

    def __init__(self, transactions: Iterable[Transaction] = ()):
        self.state = PortfolioState()
        self._transactions: dict[str, tuple[dict, Transaction]] = {}
        self.apply(transactions)

    def append(self, transaction: Transaction) -> bool:
        payload = transaction_payload(transaction)
        existing = self._transactions.get(transaction.transaction_id)
        if existing is not None:
            if existing[0] != payload:
                raise PortfolioLedgerConflict(
                    f"transaction_id reused with different content: {transaction.transaction_id}"
                )
            return False

        if transaction.action == Action.REVERSAL:
            if not transaction.reversal_of:
                raise PortfolioLedgerError("REVERSAL requires reversal_of")
            original = self._transactions.get(transaction.reversal_of)
            if original is None:
                raise PortfolioLedgerError("REVERSAL must reference an existing transaction")
            _apply_one(self.state, original[1], inverse=True)
        else:
            _apply_one(self.state, transaction)
        self._transactions[transaction.transaction_id] = (payload, transaction)
        return True

    def apply(self, transactions: Iterable[Transaction]) -> int:
        return sum(1 for transaction in transactions if self.append(transaction))

    def transactions(self) -> tuple[Transaction, ...]:
        return tuple(item[1] for item in self._transactions.values())

    def snapshot(self) -> dict:
        return self.state.as_dict()
