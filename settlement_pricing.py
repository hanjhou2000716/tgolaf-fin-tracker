"""Resolve missing trade prices from a fresh settlement quote.

The three-question form deliberately omits成交價.  Missing prices are
therefore enriched before ledger application, but stale/fallback quotes are
kept pending so a failed market-data fetch cannot silently change the account.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable, Any

from transaction_schema import Action, Transaction


@dataclass(frozen=True)
class PriceEnrichmentResult:
    accepted: tuple[Transaction, ...]
    pending: tuple[Transaction, ...]


def _quote_value(quote: Any) -> tuple[Decimal, bool, bool, str]:
    """Extract price and quality flags from a Quote-like object or number."""
    if hasattr(quote, "price"):
        raw_price = quote.price
        stale = bool(getattr(quote, "is_stale", False))
        fallback = bool(getattr(quote, "fallback_used", False))
        source = str(getattr(quote, "source", "market quote"))
    else:
        raw_price = quote
        stale = False
        fallback = False
        source = "market quote"
    try:
        price = Decimal(str(raw_price))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("quote price is not numeric") from error
    if not price.is_finite() or price <= 0:
        raise ValueError("quote price is not positive")
    return price, stale, fallback, source


def enrich_missing_trade_prices(
    transactions: Iterable[Transaction],
    quote_resolver: Callable[[Transaction], Any],
) -> PriceEnrichmentResult:
    """Attach fresh settlement estimates; return unavailable trades as pending."""
    accepted: list[Transaction] = []
    pending: list[Transaction] = []
    for transaction in transactions:
        if transaction.action not in {Action.BUY, Action.SELL} or transaction.price is not None or not transaction.approved:
            (accepted if transaction.approved else pending).append(transaction)
            continue
        try:
            quote = quote_resolver(transaction)
            price, stale, fallback, source = _quote_value(quote)
            if stale or fallback:
                raise ValueError("quote is stale or fallback")
            accepted.append(
                replace(
                    transaction,
                    price=price,
                    compatibility_used=transaction.compatibility_used
                    or f"settlement_quote_estimate:{source}",
                )
            )
        except Exception as error:
            pending.append(
                replace(
                    transaction,
                    compatibility_used=f"settlement_quote_pending:{type(error).__name__}",
                )
            )
    return PriceEnrichmentResult(tuple(accepted), tuple(pending))
