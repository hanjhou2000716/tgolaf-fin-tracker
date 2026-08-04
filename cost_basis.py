"""Deterministic cost-basis and realized P&L calculations.

Trades are fed from the validated :class:`transaction_schema.Transaction`.
Taiwan holdings use moving-average cost by default; US holdings may opt into
FIFO. Fees and taxes reduce realized P&L and are never silently discarded.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from transaction_schema import Action, Transaction


class CostBasisError(ValueError):
    pass


@dataclass
class Lot:
    quantity: Decimal
    unit_cost: Decimal


@dataclass
class Position:
    lots: list[Lot] = field(default_factory=list)
    realized_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    taxes: Decimal = Decimal("0")

    @property
    def quantity(self) -> Decimal:
        return sum((lot.quantity for lot in self.lots), Decimal("0"))

    @property
    def cost(self) -> Decimal:
        return sum((lot.quantity * lot.unit_cost for lot in self.lots), Decimal("0"))


class CostBasisEngine:
    """Apply BUY/SELL events and expose realized/unrealized P&L."""

    def __init__(self, *, us_method: str = "FIFO"):
        method = us_method.upper()
        if method not in {"FIFO", "AVERAGE"}:
            raise CostBasisError("us_method must be FIFO or AVERAGE")
        self.us_method = method
        self.positions: dict[tuple[str, str], Position] = {}

    def _position(self, tx: Transaction) -> Position:
        key = (tx.asset_type.strip(), tx.symbol.strip().upper())
        return self.positions.setdefault(key, Position())

    @staticmethod
    def _amount(value, name: str) -> Decimal:
        amount = Decimal(str(value))
        if amount <= 0:
            raise CostBasisError(f"{name} must be positive")
        return amount

    def apply(self, tx: Transaction) -> None:
        if tx.action not in {Action.BUY, Action.SELL}:
            return
        quantity = self._amount(tx.quantity, "quantity")
        price = self._amount(tx.price, "price")
        position = self._position(tx)
        is_us = tx.currency.upper() == "USD" or "美" in tx.asset_type
        method = self.us_method if is_us else "AVERAGE"
        fee = self._amount(tx.fee, "fee") if getattr(tx, "fee", None) not in (None, "", 0) else Decimal("0")
        tax = self._amount(tx.tax, "tax") if getattr(tx, "tax", None) not in (None, "", 0) else Decimal("0")
        if tx.action == Action.BUY:
            unit_cost = price + (fee + tax) / quantity
            if method == "AVERAGE" and position.quantity:
                unit_cost = (position.cost + quantity * unit_cost) / (position.quantity + quantity)
                position.lots = [Lot(position.quantity + quantity, unit_cost)]
            else:
                position.lots.append(Lot(quantity, unit_cost))
            position.fees += fee
            position.taxes += tax
            return
        if quantity > position.quantity:
            raise CostBasisError(f"sell exceeds position for {tx.symbol}")
        proceeds = quantity * price - fee - tax
        remaining = quantity
        cost = Decimal("0")
        if method == "AVERAGE":
            cost = quantity * (position.cost / position.quantity)
            position.lots = [Lot(position.quantity - quantity, position.cost / position.quantity)] if position.quantity > quantity else []
        else:
            while remaining:
                lot = position.lots[0]
                consumed = min(remaining, lot.quantity)
                cost += consumed * lot.unit_cost
                lot.quantity -= consumed
                remaining -= consumed
                if not lot.quantity:
                    position.lots.pop(0)
        position.realized_pnl += proceeds - cost
        position.fees += fee
        position.taxes += tax

    def apply_all(self, transactions: Iterable[Transaction]) -> None:
        for transaction in transactions:
            self.apply(transaction)

    def summary(self, symbol: str, asset_type: str = "", *, market_price=None) -> dict:
        position = self.positions.get((asset_type.strip(), symbol.strip().upper()), Position())
        result = {
            "symbol": symbol,
            "quantity": position.quantity,
            "cost": position.cost,
            "realizedPnl": position.realized_pnl,
            "fees": position.fees,
            "taxes": position.taxes,
        }
        if market_price is not None:
            price = Decimal(str(market_price))
            result["marketValue"] = position.quantity * price
            result["unrealizedPnl"] = result["marketValue"] - position.cost
        return result
