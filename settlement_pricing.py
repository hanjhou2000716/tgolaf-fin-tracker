"""Resolve missing trade prices from a fresh settlement quote.

The three-question form deliberately omits成交價.  Missing prices are
therefore enriched before ledger application, but stale/fallback quotes are
kept pending so a failed market-data fetch cannot silently change the account.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable, Any
from zoneinfo import ZoneInfo

from transaction_schema import Action, Transaction


@dataclass(frozen=True)
class PriceEnrichmentResult:
    accepted: tuple[Transaction, ...]
    pending: tuple[Transaction, ...]


def _quote_value(quote: Any) -> tuple[Decimal, bool, bool, str, str | None, str | None]:
    """Extract price and quality flags from a Quote-like object or number."""
    if hasattr(quote, "price"):
        raw_price = quote.price
        stale = bool(getattr(quote, "is_stale", False))
        fallback = bool(getattr(quote, "fallback_used", False))
        source = str(getattr(quote, "source", "market quote"))
        as_of = getattr(quote, "as_of", None)
        evidence_hash = getattr(quote, "evidence_hash", None)
    else:
        raw_price = quote
        stale = False
        fallback = False
        source = "market quote"
        as_of = None
        evidence_hash = None
    try:
        price = Decimal(str(raw_price))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("quote price is not numeric") from error
    if not price.is_finite() or price <= 0:
        raise ValueError("quote price is not positive")
    return price, stale, fallback, source, str(as_of) if as_of else None, str(evidence_hash) if evidence_hash else None


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
        pending_reason = ""
        try:
            quote = quote_resolver(transaction)
            price, stale, fallback, source, as_of, evidence_hash = _quote_value(quote)
            if stale or fallback:
                raise ValueError("quote is stale or fallback")
            if as_of:
                parsed_as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                if parsed_as_of.tzinfo is None:
                    parsed_as_of = parsed_as_of.replace(tzinfo=ZoneInfo("Asia/Taipei"))
                quote_date = parsed_as_of.astimezone(ZoneInfo("Asia/Taipei")).date()
                if quote_date != transaction.transaction_date:
                    pending_reason = "quote date does not match transaction date"
                    raise ValueError(pending_reason)
            accepted.append(
                replace(
                    transaction,
                    price=price,
                    compatibility_used=transaction.compatibility_used
                    or f"settlement_quote_estimate:{source}:as_of={as_of or transaction.transaction_date.isoformat()}"
                    + (f":evidence_sha256={evidence_hash}" if evidence_hash else ""),
                )
            )
        except Exception as error:
            # Only include our own fixed, non-sensitive validation reason. Provider
            # exception text can contain URLs, query strings, or credentials.
            reason = pending_reason or type(error).__name__
            pending.append(
                replace(
                    transaction,
                    compatibility_used=f"settlement_quote_pending:{reason}",
                )
            )
    return PriceEnrichmentResult(tuple(accepted), tuple(pending))
