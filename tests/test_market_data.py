import unittest
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from market_data import (
    MarketDataService,
    Quote,
    build_taiex_history_cache,
    compare_taiex_sources,
    fetch_twse_taiex_history,
    get_taiex_history,
    load_taiex_history_cache,
    parse_twse_taiex_payload,
    parse_yahoo_taiex_payload,
)


def _rows(end=date(2026, 9, 7), count=250, close=22000):
    dates = []
    cursor = end
    while len(dates) < count:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor -= timedelta(days=1)
    return [{"date": item.isoformat(), "close": close + index} for index, item in enumerate(reversed(dates))]


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


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

    def test_twse_payload_maps_explicit_chinese_fields_and_roc_date(self):
        payload = {
            "fields": ["日期", "開盤指數", "最高指數", "最低指數", "收盤指數"],
            "data": [["115/09/07", "46,000.00", "46,500.00", "45,900.00", "46,400.25"]],
        }
        self.assertEqual(parse_twse_taiex_payload(payload), [{"date": "2026-09-07", "close": 46400.25}])

    def test_yahoo_payload_maps_timestamps_and_closes(self):
        timestamp = int(datetime(2026, 9, 7, tzinfo=timezone.utc).timestamp())
        payload = {
            "chart": {"result": [{"timestamp": [timestamp], "indicators": {"quote": [{"close": [46400.25]}]}}]}
        }
        self.assertEqual(parse_yahoo_taiex_payload(payload), [{"date": "2026-09-07", "close": 46400.25}])

    def test_twse_success_does_not_call_yahoo(self):
        calls = []
        result = get_taiex_history(
            now=date(2026, 9, 7),
            twse_fetcher=lambda: _rows(),
            yahoo_fetcher=lambda: calls.append(True) or _rows(),
        )
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["source"], "TWSE")
        self.assertEqual(result["quality"], "fresh")
        self.assertEqual(calls, [])

    def test_twse_failure_falls_back_to_yahoo_chart(self):
        result = get_taiex_history(
            now=date(2026, 9, 7),
            twse_fetcher=lambda: (_ for _ in ()).throw(RuntimeError("rate limited")),
            yahoo_fetcher=lambda: _rows(),
        )
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["source"], "Yahoo Chart")
        self.assertIn("TWSE", result["fallbackReason"])

    def test_opt_in_secondary_check_blocks_material_source_mismatch(self):
        shifted = _rows()
        shifted[-1] = {**shifted[-1], "close": shifted[-1]["close"] * 1.01}
        result = get_taiex_history(
            now=date(2026, 9, 7),
            twse_fetcher=lambda: _rows(),
            yahoo_fetcher=lambda: shifted,
            compare_sources=True,
        )
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["quality"], "source_mismatch")
        self.assertEqual(result["sourceComparison"]["status"], "SOURCE_MISMATCH")

    def test_twse_monthly_fetch_uses_documented_explicit_fields(self):
        payload = {
            "fields": ["日期", "開盤指數", "最高指數", "最低指數", "收盤指數"],
            "data": [[row["date"], "1", "2", "1", str(row["close"])] for row in _rows()],
        }
        result = fetch_twse_taiex_history(
            now=date(2026, 9, 7),
            http_get=lambda *args, **kwargs: _Response(payload),
            sleep_fn=lambda _: None,
        )
        self.assertGreaterEqual(len(result), 240)
        self.assertEqual(result[-1]["date"], "2026-09-07")

    def test_source_comparison_rejects_date_or_price_mismatch(self):
        mismatch_date = compare_taiex_sources(_rows(), _rows(end=date(2026, 9, 6)))
        self.assertEqual(mismatch_date["status"], "SECONDARY_LAGGING")
        self.assertEqual(mismatch_date["comparisonDate"], "2026-09-04")
        shifted = _rows()
        shifted[-1] = {**shifted[-1], "close": shifted[-1]["close"] * 1.01}
        mismatch_price = compare_taiex_sources(_rows(), shifted)
        self.assertEqual(mismatch_price["status"], "SOURCE_MISMATCH")

    def test_twse_newer_yahoo_lag_is_ready_and_diagnostic_only(self):
        result = get_taiex_history(
            now=date(2026, 9, 7),
            twse_fetcher=lambda: _rows(end=date(2026, 9, 7)),
            yahoo_fetcher=lambda: _rows(end=date(2026, 9, 4)),
            compare_sources=True,
        )
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["source"], "TWSE")
        self.assertEqual(result["sourceComparison"]["status"], "SECONDARY_LAGGING")
        self.assertEqual(result["latestSessionDate"], "2026-09-07")
        self.assertGreaterEqual(result["rawSessionCount"], result["completedSessionCount"])

    def test_validated_cache_is_used_only_when_both_sources_fail(self):
        current = get_taiex_history(now=date(2026, 9, 7), twse_fetcher=lambda: _rows(), compare_sources=False)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "taiex-history-cache.json"
            path.write_text(json.dumps(build_taiex_history_cache(current, now=date(2026, 9, 7))), encoding="utf-8")
            result = get_taiex_history(
                now=date(2026, 9, 7),
                twse_fetcher=lambda: (_ for _ in ()).throw(RuntimeError("down")),
                yahoo_fetcher=lambda: (_ for _ in ()).throw(RuntimeError("down")),
                cache_path=path,
            )
            self.assertEqual(result["status"], "READY")
            self.assertTrue(result["cacheUsed"])
            self.assertEqual(result["quality"], "validated_cache")
            loaded, error = load_taiex_history_cache(path, now=date(2026, 9, 7))
            self.assertIsNotNone(loaded)
            self.assertIsNone(error)

    def test_cache_missing_latest_completed_session_is_rejected(self):
        current = get_taiex_history(now=date(2026, 9, 6), twse_fetcher=lambda: _rows(end=date(2026, 9, 6)), compare_sources=False)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "taiex-history-cache.json"
            path.write_text(json.dumps(build_taiex_history_cache(current, now=date(2026, 9, 6))), encoding="utf-8")
            result = get_taiex_history(
                now=date(2026, 9, 7),
                twse_fetcher=lambda: (_ for _ in ()).throw(RuntimeError("down")),
                yahoo_fetcher=lambda: (_ for _ in ()).throw(RuntimeError("down")),
                cache_path=path,
            )
            self.assertEqual(result["status"], "UNAVAILABLE")
            self.assertIn("latest expected", result["cacheValidation"])

    def test_stale_or_missing_sources_are_unavailable(self):
        stale = get_taiex_history(now=date(2026, 9, 7), twse_fetcher=lambda: _rows(end=date(2026, 8, 1)), yahoo_fetcher=lambda: _rows(end=date(2026, 8, 1)))
        self.assertEqual(stale["status"], "UNAVAILABLE")
        missing = get_taiex_history(
            now=date(2026, 9, 7),
            twse_fetcher=lambda: (_ for _ in ()).throw(RuntimeError("down")),
            yahoo_fetcher=lambda: (_ for _ in ()).throw(RuntimeError("down")),
        )
        self.assertEqual(missing["status"], "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
