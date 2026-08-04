import unittest
from datetime import datetime, timezone

from market_data import MarketDataService, Quote


class MarketDataContractTests(unittest.TestCase):
    def test_quote_contains_required_quality_fields(self):
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        service = MarketDataService(now=lambda: now)
        quote = service.get("006208", currency="TWD", fetcher=lambda _: 100.5, source="test")
        self.assertEqual(quote.price, 100.5)
        self.assertEqual(quote.currency, "TWD")
        self.assertEqual(quote.source, "test")
        self.assertFalse(quote.is_stale)
        self.assertFalse(quote.fallback_used)
        self.assertEqual(quote.quality, "ok")

    def test_fresh_cache_avoids_second_provider_call(self):
        calls = []
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        service = MarketDataService(now=lambda: now)
        fetcher = lambda _: calls.append(1) or 10
        service.get("A", currency="USD", fetcher=fetcher)
        service.get("A", currency="USD", fetcher=fetcher)
        self.assertEqual(len(calls), 1)

    def test_provider_failure_uses_stale_cache_and_marks_quality(self):
        current = [datetime(2026, 8, 4, tzinfo=timezone.utc)]
        service = MarketDataService(ttl_minutes=1, now=lambda: current[0])
        service.get("A", currency="USD", fetcher=lambda _: 10)
        current[0] = datetime(2026, 8, 4, 1, tzinfo=timezone.utc)
        quote = service.get("A", currency="USD", fetcher=lambda _: (_ for _ in ()).throw(RuntimeError("down")))
        self.assertEqual(quote.price, 10)
        self.assertTrue(quote.is_stale)
        self.assertTrue(quote.fallback_used)
        self.assertEqual(quote.quality, "stale")

    def test_quote_serialization_is_explicit(self):
        quote = Quote("A", 1, "USD", "test", "as-of", "fetched", False, False, "ok")
        self.assertEqual(set(quote.as_dict()), {"symbol", "price", "currency", "source", "as_of", "fetched_at", "is_stale", "fallback_used", "quality"})


if __name__ == "__main__":
    unittest.main()
