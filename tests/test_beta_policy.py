import json
import hashlib
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from beta_policy import (
    _canonical_hash,
    _official_action_specs,
    _official_period_matches,
    _parse_taiwan_date,
    _valid_corporate_action_evidence,
    estimate_beta_policy,
    fetch_official_taiwan_corporate_actions,
    fetch_yahoo_research_series,
    load_active_beta_policy,
    load_active_kelly_policy,
    normalize_research_price,
    official_action_cache_scope,
    validate_active_policy_document,
    validate_active_kelly_document,
    weekly_research_series,
)
from risk import remaining_beta_capacity
from risk import calculate_nav_beta
from quarterly_risk_policy import _automatic_document


def _corporate_evidence(start="2019-01-01", end="2026-09-30"):
    return {
        "provider": "Yahoo Chart API",
        "eventMapPresent": True,
        "verificationStatus": "EVENTS_VERIFIED",
        "eventCount": 1,
        "eventsHash": "a" * 64,
        "seriesHash": "b" * 64,
        "requestedStart": start,
        "requestedEnd": end,
        "verifiedAt": "2026-10-05T00:00:00Z",
        "requestedEvents": ["history", "splits", "dividends"],
    }


def _rows(values, start="2020-01-01"):
    year, month, day = (int(part) for part in start.split("-"))
    from datetime import timedelta
    anchor = date(year, month, day)
    return [{"date": (anchor + timedelta(days=index * 7)).isoformat(), "close": value} for index, value in enumerate(values)]


class BetaPolicyTests(unittest.TestCase):
    def test_official_complete_no_event_response_is_distinct_from_missing_yahoo_events(self):
        class Response:
            status_code = 200

            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        def getter(url, *, params, **kwargs):
            return Response({"stat": "ok", "tables": [{"fields": ["除權息日期", "代號", "名稱"], "data": []}]})

        result = fetch_official_taiwan_corporate_actions(
            "00886", market="otc", start=date(2021, 9, 24), end=date(2026, 9, 30), http_get=getter,
        )
        self.assertEqual(result["status"], "NO_EVENTS_VERIFIED")
        events = result["events"]
        evidence = {
            "provider": result["provider"],
            "verificationStatus": result["status"],
            "coverageComplete": result["coverageComplete"],
            "sourceFamilies": result["sourceFamilies"],
            "sourceResponseHashes": result["sourceResponseHashes"],
            "eventCount": 0,
            "eventsHash": _canonical_hash(events),
            "seriesHash": "a" * 64,
            "requestedStart": "2021-09-24",
            "requestedEnd": "2026-09-30",
            "verifiedAt": "2026-10-05T00:00:00Z",
            "requestedEvents": ["history", "splits", "dividends"],
        }
        self.assertTrue(_valid_corporate_action_evidence(
            evidence, window_start=date(2023, 9, 22), window_end=date(2026, 9, 25),
        ))
        evidence["coverageComplete"] = False
        self.assertFalse(_valid_corporate_action_evidence(evidence))

    def test_official_verified_cash_dividend_is_normalized_from_tpex_result(self):
        class Response:
            status_code = 200

            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        fields = ["除權息日期", "代號", "名稱", "除權息前收盤價", "權值+息值", "權/息", "現金股利", "每仟股無償配股"]
        dividend = ["115/04/01", "00886", "ETF", "100", "0.50", "息", "0.50", "0"]

        def getter(url, *, params, **kwargs):
            data = [dividend] if url.endswith("exDailyQ") else []
            return Response({"stat": "ok", "tables": [{"fields": fields if data else ["除權息日期", "代號", "名稱"], "data": data}]})

        result = fetch_official_taiwan_corporate_actions(
            "00886", market="otc", start=date(2021, 9, 24), end=date(2026, 9, 30), http_get=getter,
        )
        self.assertEqual(result["status"], "EVENTS_VERIFIED")
        self.assertEqual(result["events"]["dividends"], [{"date": "2026-04-01", "amount": 0.5}])

    def test_yahoo_omitted_events_are_recovered_through_official_tpex_evidence(self):
        from datetime import datetime, timezone

        class Response:
            status_code = 200

            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        sessions = [date(2026, 4, 1), date(2026, 4, 2)]
        timestamps = [int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()) for day in sessions]
        chart = {"chart": {"result": [{
            "meta": {"exchangeTimezoneName": "Asia/Taipei", "currency": "TWD"},
            "timestamp": timestamps,
            "indicators": {"quote": [{"open": [100, 99.5], "high": [100, 99.5], "low": [100, 99.5], "close": [100, 99.5]}]},
            # Yahoo omitted events entirely; a successful chart response is not enough.
        }]}}
        fields = ["除權息日期", "代號", "名稱", "除權息前收盤價", "權值+息值", "權/息", "現金股利", "每仟股無償配股"]
        dividend = ["115/04/02", "00886", "ETF", "100", "0.50", "息", "0.50", "0"]

        def getter(url, *, params, **kwargs):
            parsed_url = urlsplit(url)
            if parsed_url.hostname == "query1.finance.yahoo.com":
                return Response(chart)
            data = [dividend] if parsed_url.path.endswith("exDailyQ") else []
            return Response({"stat": "ok", "tables": [{"fields": fields if data else ["除權息日期", "代號", "名稱"], "data": data}]})

        result = fetch_yahoo_research_series(
            "00886", market="tw", start=date(2026, 4, 1), end=date(2026, 4, 3), http_get=getter,
            attempts=1,
        )
        self.assertEqual(result["corporateActionStatus"], "PASS")
        self.assertEqual(result["corporateActionEvidence"]["verificationStatus"], "EVENTS_VERIFIED")
        self.assertAlmostEqual(result["rows"][-1]["totalReturnIndex"], result["rows"][0]["totalReturnIndex"])

    def test_yahoo_fx_uses_exchange_timezone_and_crops_before_hashing(self):
        from datetime import datetime, timezone

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        # The 23:00 UTC quote is already the next calendar day in London.
        timestamps = [
            int(datetime(2020, 9, 28, 23, tzinfo=timezone.utc).timestamp()),
            int(datetime(2020, 9, 29, 23, tzinfo=timezone.utc).timestamp()),
            int(datetime(2026, 10, 1, 23, tzinfo=timezone.utc).timestamp()),
        ]
        chart = {"chart": {"result": [{
            "meta": {"exchangeTimezoneName": "Europe/London", "currency": "TWD"},
            "timestamp": timestamps,
            "indicators": {"quote": [{
                "open": [31.0, 31.0, 32.0],
                "high": [31.1, 31.2, 32.1],
                "low": [30.9, 30.9, 31.9],
                "close": [31.0, 31.1, 32.0],
            }]},
        }]}}
        calls = []

        def getter(url, *, params, **kwargs):
            calls.append(params)
            return Response(chart)

        result = fetch_yahoo_research_series(
            "TWD=X", market="fx", start=date(2020, 9, 29), end=date(2026, 9, 30),
            http_get=getter, attempts=1,
        )
        self.assertEqual([row["date"] for row in result["rows"]], ["2020-09-29", "2020-09-30"])
        self.assertEqual(result["fxEvidence"]["exchangeTimezoneName"], "Europe/London")
        self.assertEqual(result["fxEvidence"]["clippedOutOfRangeRows"], 1)
        self.assertNotEqual(result["fxEvidence"]["sourceHash"], _canonical_hash(chart))
        self.assertLess(calls[0]["period1"], int(datetime(2020, 9, 29, tzinfo=timezone.utc).timestamp()))

    def test_official_endpoint_contracts_and_roc_date_forms(self):
        _, twse = _official_action_specs("twse", date(2026, 9, 1), date(2026, 9, 30))
        _, tpex = _official_action_specs("otc", date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual(twse[0][2]["startDate"], "20260901")
        self.assertNotIn("strDate", twse[0][2])
        self.assertIn("/reducation/TWTAUU", twse[1][1])
        self.assertIn("/change/TWTB8U", twse[2][1])
        self.assertIn("/split/TWTCAU", twse[3][1])
        self.assertIn("/www/zh-tw/bulletin/exDailyQ", tpex[0][1])
        self.assertEqual(_parse_taiwan_date("115/09/30"), date(2026, 9, 30))
        self.assertEqual(_parse_taiwan_date("1150930"), date(2026, 9, 30))
        self.assertFalse(_official_period_matches({"date": "115/08/01~115/08/31"}, date(2026, 9, 1), date(2026, 9, 30)))
        self.assertTrue(_official_period_matches({"date": "115/09/01~115/09/30"}, date(2026, 9, 1), date(2026, 9, 30)))

    def test_official_query_rows_for_matching_symbol_need_parseable_dates(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        def getter(url, *, params, **kwargs):
            if url.endswith("exDailyQ"):
                return Response({"stat": "ok", "tables": [{
                    "fields": ["除權息日期", "代號", "名稱"],
                    "data": [["not-a-date", "00886", "ETF"]],
                }]})
            return Response({"stat": "ok", "tables": [{"fields": ["除權息日期", "代號", "名稱"], "data": []}]})

        result = fetch_official_taiwan_corporate_actions(
            "00886", market="otc", start=date(2021, 9, 24), end=date(2026, 9, 30), http_get=getter,
        )
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["reasonCode"], "OFFICIAL_ACTION_RESPONSE_INVALID")

    def test_official_twse_etf_split_is_converted_to_verified_ratio(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        def getter(url, *, params, **kwargs):
            if "/split/TWTCAU" in url:
                return Response({"stat": "OK", "fields": ["恢復買賣日期", "證券代號", "基金名稱", "停止買賣前最後收盤價", "恢復買賣參考價"], "data": [["2025/06/10", "00685L", "ETF", "25.5", "12.75"]]})
            return Response({"stat": "OK", "fields": ["日期", "代號", "名稱"], "data": []})

        result = fetch_official_taiwan_corporate_actions(
            "00685L", market="twse", start=date(2020, 1, 1), end=date(2026, 9, 30), http_get=getter,
        )
        self.assertEqual(result["status"], "EVENTS_VERIFIED")
        self.assertEqual(result["events"]["splits"], [{"date": "2025-06-10", "numerator": 2.0, "denominator": 1.0}])

    def test_official_range_tables_are_shared_only_inside_one_settlement_scope(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"stat": "ok", "tables": [{"fields": ["除權息日期", "代號", "名稱"], "data": []}]}

        calls = []

        def getter(url, *, params, **kwargs):
            calls.append((url, tuple(sorted(params.items()))))
            return Response()

        with official_action_cache_scope():
            a = fetch_official_taiwan_corporate_actions("00886", market="otc", start=date(2021, 1, 1), end=date(2026, 9, 30), http_get=getter)
            b = fetch_official_taiwan_corporate_actions("3455", market="otc", start=date(2021, 1, 1), end=date(2026, 9, 30), http_get=getter)
        self.assertEqual(a["status"], "NO_EVENTS_VERIFIED")
        self.assertEqual(b["status"], "NO_EVENTS_VERIFIED")
        self.assertEqual(len(calls), 3)

    def test_empty_yahoo_event_map_is_not_no_event_evidence(self):
        evidence = _corporate_evidence()
        evidence["eventCount"] = 0
        self.assertFalse(_valid_corporate_action_evidence(evidence))

    def test_split_adjusted_series_is_continuous_and_keeps_raw_close(self):
        rows = normalize_research_price(
            [{"date": "2024-01-01", "close": 100}, {"date": "2024-01-02", "close": 50}],
            splits={"0": {"date": "2024-01-02", "numerator": 2, "denominator": 1}},
        )
        self.assertEqual(rows[0]["close"], 100)
        self.assertEqual(rows[0]["splitAdjustedClose"], 50)
        self.assertEqual(rows[1]["splitAdjustedClose"], 50)

    def test_weekly_beta_aligns_by_iso_week_and_converts_usd_to_twd(self):
        benchmark = normalize_research_price([{"date": f"2024-01-{day:02d}", "close": 100 + day} for day in range(1, 29)])
        asset = normalize_research_price([{"date": f"2024-01-{day:02d}", "close": 50 + day * 2} for day in range(1, 29)])
        fx = [{"date": f"2024-01-{day:02d}", "close": 31} for day in range(1, 29)]
        result = estimate_beta_policy(
            {"TEST": {"rows": asset * 40, "currency": "USD", "source": "test", "corporateActionStatus": "PASS", "corporateActionEvidence": _corporate_evidence("2020-01-01", "2025-01-01"), "market": "us"}},
            benchmark * 40,
            fx_rows=fx * 40,
            cutoff="2024-01-31",
            min_observations=2,
        )
        self.assertIn("TEST", result["assets"])
        self.assertEqual(result["assets"]["TEST"]["status"], "CANDIDATE")

    def test_active_policy_requires_approval_and_evidence(self):
        payload = {
            "schemaVersion": 1,
            "policyVersion": "test",
            "status": "ACTIVE",
            "approvalStatus": "APPROVED",
            "dataCutoff": "2026-06-30",
            "assets": {"TEST": {"beta": 0.8, "observations": 104, "source": "test", "corporateActionStatus": "PASS"}},
        }
        betas, metadata, error = validate_active_policy_document(payload, as_of=date(2026, 9, 17))
        self.assertIsNone(error)
        self.assertEqual(betas["TEST"], 0.8)
        self.assertEqual(metadata["dataCutoff"], "2026-06-30")
        payload["assets"]["TEST"]["corporateActionStatus"] = "UNAVAILABLE"
        _, _, error = validate_active_policy_document(payload, as_of=date(2026, 9, 17))
        self.assertIsNotNone(error)

    def test_expired_quarter_policy_is_reference_only_for_one_quarter(self):
        payload = {
            "schemaVersion": 1, "policyVersion": "test-q3", "status": "ACTIVE",
            "approvalStatus": "APPROVED", "dataCutoff": "2026-06-30",
            "effectiveFromQuarter": "2026Q3", "referenceThroughQuarter": "2026Q4",
            "assets": {"TEST": {"beta": 0.8, "observations": 156, "source": "test", "corporateActionStatus": "PASS"}},
        }
        _, _, error = validate_active_policy_document(payload, as_of=date(2026, 10, 4))
        self.assertEqual(error, "policy is stale")
        betas, metadata, reference_error = validate_active_policy_document(
            payload, as_of=date(2026, 10, 4), allow_stale_reference=True,
        )
        self.assertIsNone(reference_error)
        self.assertEqual(betas["TEST"], 0.8)
        self.assertEqual(metadata["lifecycle"], "STALE_REFERENCE")
        _, _, expired_error = validate_active_policy_document(
            payload, as_of=date(2027, 1, 1), allow_stale_reference=True,
        )
        self.assertEqual(expired_error, "policy is stale")

    def test_loaders_expose_quarterly_reference_separately_from_formal_policy(self):
        beta = load_active_beta_policy("config/beta-policy-active.json", as_of=date(2026, 10, 4))
        kelly = load_active_kelly_policy("config/kelly-policy-active.json", as_of=date(2026, 10, 4))
        self.assertEqual(beta["status"], "UNAVAILABLE")
        self.assertEqual(beta["quality"], "policy_stale_reference")
        self.assertNotIn("006208", beta["betas"])
        self.assertEqual(beta["referenceMetadata"]["referenceThroughQuarter"], "2026Q4")
        self.assertEqual(kelly["status"], "UNAVAILABLE")
        self.assertEqual(kelly["referencePolicy"]["freshness"], "stale_reference")
        self.assertIsNone(load_active_beta_policy("config/beta-policy-active.json", as_of=date(2027, 1, 1)).get("referenceMetadata"))

    def test_beta_candidate_uses_only_last_three_completed_years(self):
        from datetime import timedelta

        cutoff = date(2026, 9, 30)
        first_friday = date(2019, 1, 4)
        benchmark = []
        asset = []
        day = first_friday
        index = 0
        while day <= cutoff:
            week = (day - first_friday).days // 7
            benchmark.append({"date": day.isoformat(), "splitAdjustedClose": 100 + week})
            # Earlier history is deliberately unrelated; only the trailing
            # three-year sample may affect the estimate.
            price = 100 + week * 2 if day >= date(2023, 9, 1) else 10_000 - week * 7
            asset.append({"date": day.isoformat(), "splitAdjustedClose": price})
            day += timedelta(days=7)
            index += 1
        candidate = estimate_beta_policy(
            {"TEST": {"rows": asset, "source": "fixture", "corporateActionStatus": "PASS"}},
            benchmark, cutoff=cutoff,
        )
        record = candidate["assets"]["TEST"]
        self.assertEqual(record["windowEnd"], "2026-09-25")
        self.assertGreaterEqual(record["observations"], 104)
        self.assertLess(record["observations"], 160)
        self.assertEqual(candidate["effectiveFromQuarter"], "2026Q4")

    def test_weekly_beta_does_not_bridge_multiple_missing_weeks(self):
        from datetime import timedelta

        benchmark, asset = [], []
        day = date(2022, 1, 7)
        for index in range(165):
            if not 35 <= index < 100:
                benchmark.append({"date": day.isoformat(), "splitAdjustedClose": 100 + index * 0.7 + (index % 5)})
                asset.append({"date": day.isoformat(), "splitAdjustedClose": 50 + index * 0.3 + (index % 7)})
            day += timedelta(days=7)
        result = estimate_beta_policy(
            {"TEST": {"rows": asset, "source": "fixture", "corporateActionStatus": "PASS", "corporateActionEvidence": _corporate_evidence("2021-01-01", "2026-09-30")}},
            benchmark,
            cutoff="2026-09-30",
        )
        self.assertEqual(result["assets"]["TEST"]["status"], "INSUFFICIENT_EVIDENCE")

    def test_beta_candidate_rejects_nonempty_but_stale_research_series(self):
        from datetime import timedelta

        cutoff = date(2026, 9, 30)
        expected_week = date(2026, 9, 25)
        stale_end = expected_week - timedelta(days=21)
        benchmark, asset = [], []
        for index in range(170):
            session = stale_end - timedelta(days=(169 - index) * 7)
            benchmark.append({"date": session.isoformat(), "splitAdjustedClose": 100 + index * 0.3 + (index % 5)})
            asset.append({"date": session.isoformat(), "splitAdjustedClose": 50 + index * 0.2 + (index % 7)})
        result = estimate_beta_policy(
            {"TEST": {
                "rows": asset,
                "source": "fixture",
                "corporateActionStatus": "PASS",
                "corporateActionEvidence": _corporate_evidence("2020-01-01", cutoff.isoformat()),
            }},
            benchmark,
            cutoff=cutoff,
        )
        self.assertEqual(result["assets"]["TEST"]["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result["assets"]["TEST"]["reason"], "latest paired observation is stale")

    def test_auto_validated_beta_requires_integrity_and_source_evidence(self):
        assets = {"TEST": {
            "status": "CANDIDATE", "beta": 0.8, "observations": 120,
            "source": "Yahoo Chart raw OHLC", "corporateActionStatus": "PASS",
            "corporateActionEvidence": _corporate_evidence(),
            "windowStart": "2023-09-22", "windowEnd": "2026-09-25",
        }}
        payload = {
            "schemaVersion": 1, "policyVersion": "2026Q4-test", "status": "ACTIVE",
            "approvalStatus": "AUTO_VALIDATED", "dataCutoff": "2026-09-30",
            "effectiveFromQuarter": "2026Q4", "referenceThroughQuarter": "2027Q1",
            "assets": assets,
            "contentHash": hashlib.sha256(
                json.dumps(assets, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
        payload = _automatic_document(
            payload, input_hash="c" * 64,
            validated_at=datetime.fromisoformat("2026-10-05T00:00:00+00:00"),
            validated_by="trusted-main-workflow",
        )
        betas, metadata, error = validate_active_policy_document(payload, as_of=date(2026, 10, 5))
        self.assertIsNone(error)
        self.assertEqual(betas["TEST"], 0.8)
        self.assertEqual(metadata["approvalStatus"], "AUTO_VALIDATED")
        tampered = json.loads(json.dumps(payload))
        tampered["assets"]["TEST"]["beta"] = 1.5
        _, _, error = validate_active_policy_document(tampered, as_of=date(2026, 10, 5))
        self.assertIsNotNone(error)

    def test_weekly_research_series_is_one_row_per_iso_week(self):
        rows = [
            {"date": "2025-01-02", "totalReturnIndex": 100},
            {"date": "2025-01-03", "totalReturnIndex": 101},
            {"date": "2025-01-10", "totalReturnIndex": 102},
        ]
        weekly = weekly_research_series(rows, price_key="totalReturnIndex", cutoff="2025-01-10")
        self.assertEqual([row["date"] for row in weekly], ["2025-01-03", "2025-01-10"])
        self.assertEqual(weekly[0]["close"], 101.0)

    def test_active_policy_and_kelly_files_load(self):
        beta = load_active_beta_policy("config/beta-policy-active.json", as_of=date(2026, 9, 17))
        kelly = load_active_kelly_policy("config/kelly-policy-active.json", as_of=date(2026, 9, 17))
        self.assertEqual(beta["status"], "READY")
        self.assertEqual(kelly["status"], "READY")
        self.assertAlmostEqual(kelly["policy"]["halfKellyLimit"], 0.08 / (2 * 0.18**2))

    def test_loaded_policy_produces_formal_beta_with_only_immaterial_unknowns(self):
        policy = load_active_beta_policy("config/beta-policy-active.json", as_of=date(2026, 9, 17))
        values = {"006208": 500_000, "00685L": 100_000, "QQQM": 200_000, "NVDA": 100_000, "2330": 90_000, "FUND": 2_000}
        markets = {"006208": "tw", "00685L": "tw", "QQQM": "us", "NVDA": "us", "2330": "tw", "FUND": "other"}
        result = calculate_nav_beta(values, 992_000, 100_000, policy["betas"], market_by_symbol=markets)
        self.assertEqual(result["status"], "READY")
        self.assertAlmostEqual(sum(result["contributions"].values()), result["navBeta"], places=7)

    def test_signed_remaining_capacity(self):
        self.assertAlmostEqual(remaining_beta_capacity(1.33, 1.23), -0.10)


if __name__ == "__main__":
    unittest.main()
