import json
import hashlib
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from beta_policy import (
    _as_date,
    _canonical_hash,
    _official_action_specs,
    _official_period_matches,
    _parse_taiwan_date,
    _valid_corporate_action_evidence,
    estimate_beta_policy,
    fetch_official_taiwan_corporate_actions,
    fetch_official_taiwan_daily_closes,
    fetch_research_series,
    fetch_sec_edgar_corporate_action_evidence,
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
        "priceBasis": "raw-close-plus-verified-actions-v1",
        "priceCorrections": [],
    }


def _rows(values, start="2020-01-01"):
    year, month, day = (int(part) for part in start.split("-"))
    from datetime import timedelta
    anchor = date(year, month, day)
    return [{"date": (anchor + timedelta(days=index * 7)).isoformat(), "close": value} for index, value in enumerate(values)]


class BetaPolicyTests(unittest.TestCase):
    def test_tpex_close_repair_replaces_only_anomalous_yahoo_session(self):
        from datetime import datetime, timezone

        class Response:
            status_code = 200

            def __init__(self, payload=None, text=None):
                self.payload = payload
                self.text = text

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        session = date(2023, 9, 22)
        stamp = int(datetime.combine(session, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        chart = {"chart": {"result": [{
            "meta": {"exchangeTimezoneName": "Asia/Taipei", "currency": "TWD"},
            "timestamp": [stamp],
            "indicators": {"quote": [{"open": [23.95], "high": [23.96], "low": [23.90], "close": [23.97]}]},
            "events": {},
        }]}}
        calls = []

        def getter(url, *, params=None, **kwargs):
            calls.append((url, params))
            if "query1.finance.yahoo.com" in url:
                return Response(chart)
            if url.endswith("tradingStock"):
                return Response({"stat": "ok", "tables": [{
                    "fields": ["日期", "代號", "名稱", "開盤", "最高", "最低", "收盤"],
                    "data": [["112/09/22", "00886", "ETF", "23.90", "23.96", "23.89", "23.96"]],
                }]})
            if "tpex.org.tw" in url:
                return Response({"stat": "ok", "tables": [{"fields": ["除權息日期", "代號", "名稱"], "data": []}]})
            self.fail(f"unexpected source URL: {url}")

        result = fetch_yahoo_research_series(
            "00886", market="tw", start=session, end=session, http_get=getter, attempts=1,
        )
        self.assertEqual(result.get("status"), "READY", result)
        self.assertEqual(result["rows"][0]["close"], 23.96)
        self.assertEqual(result["rows"][0]["yahooClose"], 23.97)
        correction = result["priceEvidence"]["corrections"][0]
        self.assertEqual(correction["provider"], "TPEx official daily close")
        self.assertEqual(correction["sourceHash"], _canonical_hash({"stat": "ok", "tables": [{
            "fields": ["日期", "代號", "名稱", "開盤", "最高", "最低", "收盤"],
            "data": [["112/09/22", "00886", "ETF", "23.90", "23.96", "23.89", "23.96"]],
        }]}))
        self.assertTrue(_valid_corporate_action_evidence(result["corporateActionEvidence"]))
        self.assertTrue(any(url.endswith("tradingStock") and params["code"] == "00886" for url, params in calls))

    def test_twse_pure_rights_uses_official_reference_price_once(self):
        class Response:
            status_code = 200

            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        fields = ["資料日期", "股票代號", "股票名稱", "除權息前收盤價", "除權息參考價", "權值+息值", "權/息"]
        rows = [
            ["114年02月18日", "8033", "雷虎", "66.70", "65.59", "1.107255", "權"],
            ["115年05月14日", "8033", "雷虎", "140.00", "137.66", "2.332507", "權"],
        ]

        def getter(url, *, params=None, **kwargs):
            if url.endswith("TWT49U"):
                return Response({"stat": "OK", "fields": fields, "data": rows})
            return Response({"stat": "查詢日期範圍內無資料", "fields": ["資料日期", "股票代號"], "data": []})

        result = fetch_official_taiwan_corporate_actions(
            "8033", market="twse", start=date(2024, 9, 30), end=date(2026, 9, 30), http_get=getter,
        )
        self.assertEqual(result["status"], "EVENTS_VERIFIED", result)
        adjustments = result["events"]["splits"]
        self.assertEqual(len(adjustments), 2)
        self.assertAlmostEqual(adjustments[0]["factor"], 65.59 / 66.70)
        self.assertAlmostEqual(adjustments[1]["factor"], 137.66 / 140.0)
        adjusted = normalize_research_price(
            [{"date": "2025-02-17", "close": 66.70}, {"date": "2025-02-18", "close": 65.59}],
            splits=adjustments[:1],
        )
        self.assertAlmostEqual(adjusted[0]["splitAdjustedClose"], 65.59)
        self.assertAlmostEqual(adjusted[1]["splitAdjustedClose"], 65.59)

    def test_sec_window_can_verify_no_events_only_with_complete_filing_documents(self):
        class Response:
            status_code = 200

            def __init__(self, payload=None, text=None):
                self.payload, self.text = payload, text

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        report_dates = [
            "2023-09-30", "2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
            "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30",
        ]
        arrays = {"accessionNumber": [], "filingDate": [], "form": [], "primaryDocument": [], "reportDate": []}
        for index, report_date in enumerate(report_dates):
            year, month, _ = (int(part) for part in report_date.split("-"))
            form = "10-K" if month == 12 else "10-Q"
            filing_date = (date.fromisoformat(report_date) + timedelta(days=35)).isoformat()
            arrays["accessionNumber"].append(f"0001628280-{str(year)[-2:]}-{index + 1:06d}")
            arrays["filingDate"].append(filing_date)
            arrays["form"].append(form)
            arrays["primaryDocument"].append(f"report-{index}.htm")
            arrays["reportDate"].append(report_date)
        arrays["accessionNumber"].append("0001628280-26-000099")
        arrays["filingDate"].append("2026-05-20")
        arrays["form"].append("8-K")
        arrays["primaryDocument"].append("event.htm")
        arrays["reportDate"].append("2026-05-20")
        submissions = {"cik": 1318605, "filings": {"recent": arrays}}

        def getter(url, *, headers=None, **kwargs):
            if url.endswith("submissions/CIK0001318605.json"):
                return Response(submissions)
            return Response(text="Tesla quarterly filing. No corporate action was announced during this period.")

        evidence = fetch_sec_edgar_corporate_action_evidence(
            "TSLA", start=date(2023, 9, 22), end=date(2026, 9, 30), http_get=getter,
        )
        self.assertEqual(evidence["status"], "NO_EVENTS_VERIFIED", evidence)
        self.assertGreaterEqual(evidence["reviewedFilingCount"], 10)
        self.assertIn("8-K", evidence["sourceFamilies"])
        self.assertGreaterEqual(len(evidence["sourceResponseHashes"]), 2)
        evidence.update({
            "eventsHash": _canonical_hash(evidence["events"]), "seriesHash": "b" * 64,
            "requestedEvents": ["history", "splits", "dividends"],
            "verificationStatus": evidence["status"],
            "priceBasis": "raw-close-plus-verified-actions-v1", "priceCorrections": [],
        })
        self.assertTrue(_valid_corporate_action_evidence(
            evidence, window_start=date(2023, 9, 22), window_end=date(2026, 9, 25),
        ), evidence)
        tampered = dict(evidence)
        tampered["events"] = dict(evidence["events"])
        tampered["events"]["reviewManifest"] = list(evidence["events"]["reviewManifest"])
        tampered["events"]["reviewManifest"][0] = dict(
            tampered["events"]["reviewManifest"][0],
            documentUrl="https://www.sec.gov/Archives/edgar/data/1318605/000162828026999999/report-0.htm",
        )
        self.assertFalse(_valid_corporate_action_evidence(
            tampered, window_start=date(2023, 9, 22), window_end=date(2026, 9, 25),
        ))
        tampered = dict(evidence)
        tampered["events"] = dict(evidence["events"])
        tampered["events"]["reviewManifest"] = [dict(item) for item in evidence["events"]["reviewManifest"]]
        tampered["events"]["reviewManifest"][0]["filingDate"] = "2020-01-01"
        self.assertFalse(_valid_corporate_action_evidence(
            tampered, window_start=date(2023, 9, 22), window_end=date(2026, 9, 25),
        ))
        tampered = dict(evidence)
        tampered["sourceResponseHashes"] = dict(evidence["sourceResponseHashes"])
        tampered["sourceResponseHashes"].pop(next(iter(evidence["events"]["reviewManifest"]))["accession"])
        self.assertFalse(_valid_corporate_action_evidence(
            tampered, window_start=date(2023, 9, 22), window_end=date(2026, 9, 25),
        ))

        # Enough filings can still leave a hole in the issuer's quarterly coverage.
        remove_dates = {"2024-03-31", "2024-06-30"}
        keep = [index for index, report_date in enumerate(arrays["reportDate"]) if report_date not in remove_dates]
        for name, values in arrays.items():
            arrays[name] = [values[index] for index in keep]
        incomplete_coverage = fetch_sec_edgar_corporate_action_evidence(
            "TSLA", start=date(2023, 9, 22), end=date(2026, 9, 30), http_get=getter,
        )
        self.assertEqual(incomplete_coverage["status"], "UNVERIFIED")
        self.assertIn("do not cover", incomplete_coverage["reason"])

    def test_sec_possible_action_disclosure_and_incomplete_filing_set_stay_unverified(self):
        class Response:
            status_code = 200

            def __init__(self, payload=None, text=None):
                self.payload, self.text = payload, text

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        incomplete = {"cik": 1318605, "filings": {"recent": {
            "accessionNumber": [], "filingDate": [], "form": [], "primaryDocument": [], "reportDate": [],
        }}}
        result = fetch_sec_edgar_corporate_action_evidence(
            "TSLA", start=date(2023, 9, 22), end=date(2026, 9, 30),
            http_get=lambda *args, **kwargs: Response(incomplete),
        )
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["reasonCode"], "US_ACTION_EVIDENCE_UNVERIFIED")

    def test_sec_zero_event_manifest_survives_yahoo_series_normalization(self):
        from datetime import timezone

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        start, end = date(2023, 9, 22), date(2026, 9, 30)
        manifest = []
        hashes = {"submissions": "f" * 64}
        forms = ["10-K", "10-Q", "10-Q", "8-K"] * 3
        for index, form in enumerate(forms):
            accession = f"0001318605-26-{index + 1:06d}"
            digest = f"{index + 1:064x}"
            hashes[accession] = digest
            manifest.append({
                "form": form, "accession": accession, "filingDate": "2026-09-01",
                "reportDate": None if form == "8-K" else "2026-06-30",
                "documentUrl": f"https://www.sec.gov/Archives/edgar/data/1318605/{accession.replace('-', '')}/report.htm",
                "contentHash": digest, "reviewStatus": "NO_ACTION_DISCLOSURE",
            })
        sec_result = {
            "status": "NO_EVENTS_VERIFIED", "provider": "SEC EDGAR issuer filings",
            "issuerCik": "0001318605", "events": {"splits": [], "dividends": [], "reviewManifest": manifest},
            "sourceFamilies": ["submissions", "10-K", "10-Q", "8-K"],
            "sourceResponseHashes": hashes, "eventCount": 0, "reviewedFilingCount": len(manifest),
            "coverageComplete": True, "requestedStart": start.isoformat(), "requestedEnd": end.isoformat(),
            "verifiedAt": "2026-10-05T00:00:00Z",
        }
        session = date(2026, 9, 25)
        timestamp = int(datetime.combine(session, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        chart = {"chart": {"result": [{
            "meta": {"exchangeTimezoneName": "America/New_York", "currency": "USD"},
            "timestamp": [timestamp],
            "indicators": {"quote": [{"open": [100], "high": [101], "low": [99], "close": [100]}]},
        }]}}
        with patch("beta_policy.fetch_sec_edgar_corporate_action_evidence", return_value=sec_result):
            result = fetch_yahoo_research_series(
                "TSLA", market="us", start=start, end=end,
                http_get=lambda *args, **kwargs: Response(chart), attempts=1,
            )
        self.assertEqual(result["corporateActionStatus"], "PASS")
        self.assertEqual(len(result["corporateActionEvents"]["reviewManifest"]), len(manifest))
        self.assertTrue(_valid_corporate_action_evidence(
            result["corporateActionEvidence"], window_start=start, window_end=date(2026, 9, 25),
        ), result["corporateActionEvidence"])

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
            "priceBasis": "raw-close-plus-verified-actions-v1",
            "priceCorrections": [],
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
                # FX quotes are validated as dated positive closes, not as
                # listed-security OHLC envelopes.
                "high": [30.8, 31.2, 32.1],
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

    def test_cbc_fx_fallback_uses_one_complete_range_when_yahoo_fails(self):
        class Response:
            def __init__(self, payload=None, error=None):
                self.payload, self.error = payload, error

            def raise_for_status(self):
                if self.error:
                    raise self.error

            def json(self):
                return self.payload

        calls = []

        def getter(url, **kwargs):
            calls.append(url)
            if "yahoo" in url:
                return Response(error=RuntimeError("upstream unavailable"))
            return Response([{"日期": "20260928", "NTD_USD": "31.2"}, {"日期": "20260929", "NTD_USD": "31.3"}])

        result = fetch_research_series(
            "TWD=X", market="fx", start=date(2026, 9, 28), end=date(2026, 9, 30), http_get=getter,
        )
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["fxEvidence"]["provider"], "Taiwan CBC OpenData")
        self.assertIn("RuntimeError", result["fxEvidence"]["primaryFailure"])
        self.assertEqual([row["close"] for row in result["rows"]], [31.2, 31.3])
        self.assertEqual(sum("yahoo" in url for url in calls), 3)
        self.assertEqual(sum("cbc.gov.tw" in url for url in calls), 1)

    def test_compact_cbc_session_date_is_python_310_compatible(self):
        self.assertEqual(_as_date("20260928"), date(2026, 9, 28))
        self.assertIsNone(_as_date("20260230"))

    def test_official_endpoint_contracts_and_roc_date_forms(self):
        _, twse = _official_action_specs("twse", date(2026, 9, 1), date(2026, 9, 30))
        _, tpex = _official_action_specs("otc", date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual(twse[0][2]["startDate"], "20260901")
        self.assertNotIn("strDate", twse[0][2])
        self.assertIn("/reducation/TWTAUU", twse[1][1])
        self.assertIn("/change/TWTB8U", twse[2][1])
        self.assertIn("/split/TWTCAU", twse[3][1])
        self.assertIn("/www/zh-tw/bulletin/exDailyQ", tpex[0][1])
        self.assertEqual(tpex[0][2]["startDate"], "2026/09/01")
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

    def test_split_event_list_and_mapping_use_the_same_ratio_contract(self):
        raw = [{"date": "2024-01-01", "close": 100}, {"date": "2024-01-02", "close": 50}]
        as_mapping = normalize_research_price(raw, splits={"0": {"date": "2024-01-02", "numerator": 2, "denominator": 1}})
        as_list = normalize_research_price(raw, splits=[{"date": "2024-01-02", "numerator": 2, "denominator": 1}])
        self.assertEqual(as_mapping, as_list)

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
        self.assertEqual(result["assets"]["TEST"]["status"], "CANDIDATE", result["assets"]["TEST"])

    def test_fx_coverage_is_checked_only_for_paired_weeks_in_three_year_window(self):
        cutoff = date(2026, 9, 30)
        benchmark, asset, fx = [], [], []
        first_friday = date(2020, 1, 3)
        day = first_friday
        index = 0
        while day <= date(2026, 9, 25):
            benchmark.append({"date": day.isoformat(), "splitAdjustedClose": 100 + index * 0.3 + (index % 5)})
            asset.append({"date": day.isoformat(), "splitAdjustedClose": 60 + index * 0.4 + (index % 7)})
            # Deliberately omit all pre-window FX history. The estimator must
            # not require rates for rows it will not use.
            if day >= date(2023, 9, 29):
                fx.append({"date": day.isoformat(), "close": 31 + index * 0.001})
            day += timedelta(days=7)
            index += 1
        series = {"rows": asset, "currency": "USD", "source": "Yahoo USD equity", "corporateActionStatus": "PASS",
                  "corporateActionEvidence": _corporate_evidence("2020-01-03", "2026-09-25")}
        result = estimate_beta_policy({"TEST": series}, benchmark, fx_rows=fx, cutoff=cutoff)
        self.assertEqual(result["assets"]["TEST"]["status"], "CANDIDATE", result["assets"]["TEST"])

        missing_one_current_week = [row for row in fx if row["date"] not in {"2025-01-31", "2025-02-07"}]
        blocked = estimate_beta_policy({"TEST": series}, benchmark, fx_rows=missing_one_current_week, cutoff=cutoff)
        self.assertEqual(blocked["assets"]["TEST"]["status"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("paired weeks inside", blocked["assets"]["TEST"]["reason"])

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

