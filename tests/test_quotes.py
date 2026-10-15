import unittest

from quotes import QuoteUnavailableError, get_tw_stock_close_on_date, get_tw_stock_price, yahoo_market_symbols


class FakeResponse:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {}
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


class QuoteFallbackTests(unittest.TestCase):
    def test_historical_close_requires_exact_transaction_date(self):
        calls = []
        def http_get(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse({"data": [{"date": "2026-10-15", "close": 23.96}]})
        result = get_tw_stock_close_on_date("00685L", "2026-10-15", "token", http_get=http_get)
        self.assertEqual(result["price"], 23.96)
        self.assertEqual(result["source"], "FinMind TaiwanStockPrice")
        self.assertEqual(result["date"], "2026-10-15")
        self.assertEqual(len(result["evidence_hash"]), 64)
        self.assertEqual(calls[0][1]["params"]["start_date"], "2026-10-15")
        self.assertEqual(calls[0][1]["params"]["end_date"], "2026-10-15")

    def test_historical_close_does_not_accept_a_neighboring_date(self):
        def http_get(url, **kwargs):
            if "finmindtrade" in url:
                return FakeResponse({"data": [{"date": "2026-10-14", "close": 23.96}]})
            raise RuntimeError("provider unavailable")

        class EmptyTicker:
            def history(self, **kwargs):
                class EmptyPrices:
                    empty = True
                    def dropna(self): return self
                return {"Close": EmptyPrices()}

        with self.assertRaisesRegex(QuoteUnavailableError, "on 2026-10-15"):
            get_tw_stock_close_on_date(
                "00685L", "2026-10-15", "token", http_get=http_get,
                ticker_factory=lambda _: EmptyTicker(),
            )

    def test_historical_close_rejects_non_finite_price(self):
        def http_get(url, **kwargs):
            if "finmindtrade" in url:
                return FakeResponse({"data": [{"date": "2026-10-15", "close": float("inf")}]})
            raise RuntimeError("provider unavailable")

        class EmptyTicker:
            def history(self, **kwargs):
                class EmptyPrices:
                    empty = True
                    def dropna(self): return self
                return {"Close": EmptyPrices()}

        with self.assertRaisesRegex(QuoteUnavailableError, "exact-date close unavailable"):
            get_tw_stock_close_on_date(
                "00685L", "2026-10-15", "token", http_get=http_get,
                ticker_factory=lambda _: EmptyTicker(),
            )

    def test_finmind_retry_then_success(self):
        responses = [
            FakeResponse({"data": []}),
            FakeResponse({"data": [{"close": 18.25}]}),
        ]
        price = get_tw_stock_price(
            "00886", "token", http_get=lambda *args, **kwargs: responses.pop(0), sleep=lambda _: None
        )
        self.assertEqual(price, 18.25)

    def test_yahoo_chart_fallback(self):
        responses = [
            FakeResponse({"data": []}), FakeResponse({"data": []}),
            FakeResponse({"chart": {"result": [{"indicators": {"quote": [{"close": [None, 19.8]}]}}]}}),
        ]
        urls = []
        def http_get(url, **kwargs):
            urls.append(url)
            return responses.pop(0)
        price = get_tw_stock_price("00886", "token", http_get=http_get, sleep=lambda _: None)
        self.assertEqual(price, 19.8)
        self.assertIn("00886.TWO", urls[-1])

    def test_uses_otc_suffix_before_listing_suffix(self):
        self.assertEqual(yahoo_market_symbols("00886"), ("00886.TWO", "00886.TW"))
        self.assertEqual(yahoo_market_symbols("006208"), ("006208.TW", "006208.TWO"))

    def test_reports_all_sources_unavailable(self):
        responses = [
            FakeResponse({"data": []}), FakeResponse({"data": []}),
            FakeResponse({"chart": {"result": [{"indicators": {"quote": [{"close": []}]}}]}}),
            FakeResponse({"chart": {"result": [{"indicators": {"quote": [{"close": []}]}}]}}),
        ]

        class EmptyTicker:
            def history(self, period):
                class EmptyPrices:
                    empty = True
                    def dropna(self): return self
                return {"Close": EmptyPrices()}

        with self.assertRaisesRegex(QuoteUnavailableError, "Taiwan quote unavailable for 00886"):
            get_tw_stock_price(
                "00886", "token", http_get=lambda *args, **kwargs: responses.pop(0),
                ticker_factory=lambda _: EmptyTicker(), sleep=lambda _: None,
            )


if __name__ == "__main__":
    unittest.main()
