import unittest

from quotes import QuoteUnavailableError, get_tw_stock_price


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
        price = get_tw_stock_price(
            "00886", "token", http_get=lambda *args, **kwargs: responses.pop(0), sleep=lambda _: None
        )
        self.assertEqual(price, 19.8)

    def test_reports_all_sources_unavailable(self):
        responses = [FakeResponse({"data": []}), FakeResponse({"data": []}), FakeResponse({"chart": {"result": [{"indicators": {"quote": [{"close": []}]}}]}})]

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
