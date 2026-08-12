"""Unified quote contract with cache and stale-while-revalidate semantics."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from quotes import get_tw_stock_price


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    currency: str
    source: str
    as_of: str
    fetched_at: str
    is_stale: bool
    fallback_used: bool
    quality: str

    def as_dict(self):
        return self.__dict__.copy()


class MarketDataService:
    def __init__(self, *, ttl_minutes=90, now: Callable[[], datetime] | None = None):
        self.ttl = timedelta(minutes=ttl_minutes)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._cache: dict[str, Quote] = {}

    def _fresh(self, quote: Quote, now: datetime) -> bool:
        try:
            fetched = datetime.fromisoformat(quote.fetched_at.replace("Z", "+00:00"))
            return now - fetched <= self.ttl
        except ValueError:
            return False

    def get(self, symbol, *, currency, fetcher, source="provider") -> Quote:
        symbol = str(symbol).strip().upper()
        now = self._now()
        cached = self._cache.get(symbol)
        if cached and self._fresh(cached, now):
            return cached
        try:
            raw = fetcher(symbol)
            if isinstance(raw, Quote):
                quote = raw
            else:
                quote = Quote(
                    symbol=symbol,
                    price=float(raw),
                    currency=currency,
                    source=source,
                    as_of=now.isoformat(),
                    fetched_at=now.isoformat(),
                    is_stale=False,
                    fallback_used=False,
                    quality="ok",
                )
            self._cache[symbol] = quote
            return quote
        except Exception:
            if cached:
                stale = Quote(
                    **{**cached.as_dict(), "is_stale": True, "fallback_used": True, "quality": "stale"}
                )
                self._cache[symbol] = stale
                return stale
            raise

    def get_taiwan(self, symbol, finmind_token):
        return self.get(
            symbol,
            currency="TWD",
            source="FinMind/Yahoo/yfinance",
            fetcher=lambda item: get_tw_stock_price(item, finmind_token),
        )

    def get_fx(self, pair, fetcher, source="provider"):
        """Return the same quality-aware contract used for equity quotes."""
        return self.get(pair, currency="TWD", fetcher=fetcher, source=source)
