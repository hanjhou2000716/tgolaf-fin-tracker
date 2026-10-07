import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from beta_policy import _canonical_hash, load_active_beta_policy, load_active_kelly_policy
from quarterly_risk_policy import (
    _candidate_cache,
    _load_research,
    _save_research,
    _sealed_cache,
    active_policy_path,
    ensure_quarterly_risk_policies,
    inventory_symbol_markets,
    promote_beta_candidate_if_qualified,
)
from quarterly_risk_validation import validate_snapshot_readonly


def _research_series(symbol, market, start, cutoff):
    last_friday = cutoff - timedelta(days=(cutoff.weekday() - 4) % 7)
    rows = []
    for index in range(330):
        session = last_friday - timedelta(days=(329 - index) * 7)
        if symbol == "006208":
            price = 100 + index * 0.29 + (index % 9) * 0.8
        else:
            price = 70 + index * 0.22 + (index % 7) * 0.9
        rows.append({
            "date": session.isoformat(),
            "close": price,
            "splitAdjustedClose": price,
            "totalReturnIndex": price,
        })
    series_hash = _canonical_hash(rows)
    if symbol == "TWD=X":
        return {
            "symbol": symbol,
            "market": "fx",
            "currency": "TWD_PER_USD",
            "quotePair": "USD/TWD",
            "rows": rows,
            "source": "Yahoo Chart raw FX fixture",
            "seriesHash": series_hash,
            "fxEvidence": {
                "provider": "Yahoo Chart API",
                "baseCurrency": "USD",
                "quoteCurrency": "TWD",
                "requestedStart": start.isoformat(),
                "requestedEnd": cutoff.isoformat(),
                "verifiedAt": "2026-10-05T01:00:00Z",
                "coverageComplete": True,
                "sourceHash": series_hash,
                "exchangeTimezoneName": "Europe/London",
                "clippedOutOfRangeRows": 0,
                "validationMethod": "fx-close-v1",
            },
        }
    events = {"splits": {"fixture": {"ratio": 1.0}}, "dividends": {}}
    evidence = {
        "provider": "Yahoo Chart API",
        "eventMapPresent": True,
        "verificationStatus": "EVENTS_VERIFIED",
        "eventCount": 1,
        "eventsHash": _canonical_hash(events),
        "seriesHash": series_hash,
        "requestedStart": start.isoformat(),
        "requestedEnd": cutoff.isoformat(),
        "verifiedAt": "2026-10-05T01:00:00Z",
        "requestedEvents": ["history", "splits", "dividends"],
        "eventCounts": {"splits": 0, "dividends": 0},
    }
    return {
        "symbol": symbol,
        "market": market,
        "currency": "USD" if market == "us" else "TWD",
        "rows": rows,
        "source": "Yahoo Chart raw OHLC fixture",
        "corporateActionStatus": "PASS",
        "corporateActionEvidence": evidence,
        "corporateActionEvents": events,
        "seriesHash": series_hash,
    }


class QuarterlyRiskPolicyTests(unittest.TestCase):
    def test_inventory_symbols_are_unique_and_ignore_cash_and_collateral(self):
        symbols = inventory_symbol_markets({
            "台股": {"006208": 100, "2330": 20, "History": []},
            "美股": {"AAPL": 10},
            "基金": {"FUND": 1},
            "擔保品": {"006208": 100},
            "現金_TWD": {"TWD": 5_000},
        })
        self.assertEqual(symbols, {"006208": "tw", "2330": "tw", "AAPL": "us", "FUND": "other"})

    def test_quarter_candidates_auto_validate_and_beta_waits_for_live_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            candidate_dir = root / "audit"
            calls = []

            def fetcher(symbol, *, market, start, end, token=None):
                calls.append(symbol)
                return _research_series(symbol, market, start, end)

            inventory = {"台股": {"006208": 100, "TEST": 50}, "美股": {}, "基金": {}, "擔保品": {"TEST": 50}}
            summary = ensure_quarterly_risk_policies(
                inventory,
                state_dir=state_dir,
                candidate_dir=candidate_dir,
                today=date(2026, 10, 5),
                now=datetime(2026, 10, 5, tzinfo=timezone.utc),
                fetcher=fetcher,
            )
            self.assertEqual(summary["dataCutoff"], "2026-09-30")
            self.assertEqual(summary["completedWeekCutoff"], "2026-09-25")
            self.assertTrue((candidate_dir / "beta-policy-candidate.json").exists())
            self.assertEqual(load_active_beta_policy(state_dir / "beta-policy-active.json", as_of=date(2026, 10, 5))["status"], "UNAVAILABLE")
            kelly = load_active_kelly_policy(state_dir / "kelly-policy-active.json", as_of=date(2026, 10, 5))
            self.assertEqual(kelly["status"], "READY")
            self.assertEqual(kelly["policy"]["approvalStatus"], "AUTO_VALIDATED")

            blocked = promote_beta_candidate_if_qualified(
                {"006208": 800_000, "TEST": 100_000, "UNKNOWN": 100_000},
                1_000_000,
                0,
                market_by_symbol={"006208": "tw", "TEST": "tw", "UNKNOWN": "tw"},
                state_dir=state_dir,
                today=date(2026, 10, 5),
                now=datetime(2026, 10, 5, tzinfo=timezone.utc),
            )
            self.assertEqual(blocked["status"], "WAITING_FOR_PORTFOLIO_QUALIFICATION")
            self.assertFalse((state_dir / "beta-policy-active.json").exists())

            activated = promote_beta_candidate_if_qualified(
                {"006208": 900_000, "TEST": 100_000},
                1_000_000,
                0,
                market_by_symbol={"006208": "tw", "TEST": "tw"},
                state_dir=state_dir,
                today=date(2026, 10, 5),
                now=datetime(2026, 10, 5, tzinfo=timezone.utc),
            )
            self.assertEqual(activated["status"], "AUTO_ACTIVATED")
            beta = load_active_beta_policy(state_dir / "beta-policy-active.json", as_of=date(2026, 10, 5))
            self.assertEqual(beta["status"], "READY")
            self.assertEqual(beta["metadata"]["approvalStatus"], "AUTO_VALIDATED")
            self.assertEqual(
                Path(active_policy_path(state_dir, "beta-policy-active.json", "config/beta-policy-active.json")),
                state_dir / "beta-policy-active.json",
            )
            self.assertAlmostEqual(beta["betas"]["006208"], 1.0)

            previous_calls = list(calls)
            second = ensure_quarterly_risk_policies(
                inventory,
                state_dir=state_dir,
                candidate_dir=candidate_dir,
                today=date(2026, 10, 5),
                now=datetime(2026, 10, 5, 1, tzinfo=timezone.utc),
                fetcher=fetcher,
            )
            self.assertEqual(calls, previous_calls)
            self.assertEqual(second["beta"]["status"], "REUSED")
            self.assertEqual(len(list((state_dir / "research" / "2026-09-30").glob("*.json"))), 2)

    def test_corrupt_research_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            history = _research_series("006208", "tw", date(2020, 9, 29), date(2026, 9, 30))
            from quarterly_risk_policy import _save_research, _load_research

            _save_research(state, date(2026, 9, 30), "006208", {**history, "status": "READY"})
            cache = state / "research" / "2026-09-30" / "006208.json"
            payload = json.loads(cache.read_text(encoding="utf-8"))
            payload["series"]["rows"][0]["close"] += 1
            cache.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(_load_research(state, date(2026, 9, 30), "006208"))

    def test_fx_cache_uses_currency_pair_contract_not_stock_action_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            cutoff = date(2026, 9, 30)
            fx = _research_series("TWD=X", "fx", cutoff - timedelta(days=2400), cutoff)
            _save_research(state, cutoff, "TWD=X", {**fx, "status": "READY"})
            self.assertIsNotNone(_load_research(state, cutoff, "TWD=X"))
            cache = state / "research" / cutoff.isoformat() / "TWD_X.json"
            payload = json.loads(cache.read_text(encoding="utf-8"))
            payload["series"]["quotePair"] = "TWD/USD"
            payload = _sealed_cache({key: value for key, value in payload.items() if key != "cacheHash"})
            cache.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(_load_research(state, cutoff, "TWD=X"))

    def test_yahoo_fx_missing_required_weeks_switches_to_whole_cbc_series(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cutoff = date(2026, 9, 30)
            calls = []

            def fetcher(symbol, *, market, start, end, token=None):
                if symbol == "TWD=X":
                    primary = _research_series(symbol, market, start, end)
                    primary["rows"] = [
                        row for row in primary["rows"]
                        if start.isoformat() <= row["date"] <= end.isoformat()
                    ][::3]
                    primary["seriesHash"] = _canonical_hash(primary["rows"])
                    primary["fxEvidence"]["sourceHash"] = primary["seriesHash"]
                    calls.append("Yahoo")
                    return primary
                return _research_series(symbol, market, start, end)

            def cbc_fallback(*, start, end):
                history = _research_series("TWD=X", "fx", start, end)
                history["rows"] = [
                    row for row in history["rows"]
                    if start.isoformat() <= row["date"] <= end.isoformat()
                ]
                history["seriesHash"] = _canonical_hash(history["rows"])
                history["source"] = "Taiwan CBC FTDOpenData_Day fixture"
                history["fxEvidence"].update({
                    "provider": "Taiwan CBC OpenData",
                    "closingConvention": "Taiwan interbank daily close",
                    "validationMethod": "fx-close-v1",
                    "sourceHash": history["seriesHash"],
                })
                calls.append("CBC")
                return history

            summary = ensure_quarterly_risk_policies(
                {"台股": {"006208": 100}, "美股": {"AAPL": 20}, "基金": {}},
                state_dir=root / "state", candidate_dir=root / "audit",
                today=date(2026, 10, 5), now=datetime(2026, 10, 5, tzinfo=timezone.utc),
                fetcher=fetcher, fx_fallback_fetcher=cbc_fallback,
            )
            self.assertEqual(calls, ["Yahoo", "CBC"])
            candidate = json.loads((root / "audit" / "beta-policy-candidate.json").read_text(encoding="utf-8"))
            self.assertEqual(candidate["assets"]["AAPL"]["status"], "CANDIDATE",
                             {"asset": candidate["assets"]["AAPL"], "fx": candidate.get("fxSourceSelection"), "calls": calls})
            self.assertIn("Taiwan CBC", candidate["fxSourceSelection"]["source"])
            self.assertEqual(candidate["fxSourceSelection"]["fallbackReason"],
                             "Yahoo FX did not cover required completed paired weeks")
            restored_fx = _load_research(root / "state", cutoff, "TWD=X")
            self.assertIn("Taiwan CBC", restored_fx["source"])
            self.assertEqual(summary["beta"]["symbolsUnresolved"], [])

    def test_v2_candidate_cache_is_rejected_after_algorithm_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            payload = _sealed_cache({
                "dataCutoff": "2026-09-30", "algorithmVersion": "quarterly-risk-v2", "assets": {},
            })
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(_candidate_cache(path, cutoff=date(2026, 9, 30)))

    def test_v4_recent_failure_does_not_cool_down_v5_candidate_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir, candidate_dir = root / "state", root / "audit"
            state_dir.mkdir()
            failed = {
                "status": "INSUFFICIENT_EVIDENCE",
                "reason": "stale v4 result",
                "attemptedAt": "2026-10-07T00:00:00Z",
                "observations": 0,
            }
            old = _sealed_cache({
                "dataCutoff": "2026-09-30",
                "algorithmVersion": "quarterly-risk-v4",
                "assets": {"TEST": failed},
            })
            (state_dir / "beta-policy-candidate-cache.json").write_text(json.dumps(old), encoding="utf-8")
            calls = []

            def fetcher(symbol, *, market, start, end, token=None):
                calls.append(symbol)
                return _research_series(symbol, market, start, end)

            summary = ensure_quarterly_risk_policies(
                {"台股": {"006208": 100, "TEST": 50}, "美股": {}, "基金": {}},
                state_dir=state_dir,
                candidate_dir=candidate_dir,
                today=date(2026, 10, 7),
                now=datetime(2026, 10, 7, tzinfo=timezone.utc),
                fetcher=fetcher,
            )
            self.assertIn("TEST", calls)
            self.assertEqual(summary["algorithmVersion"], "quarterly-risk-v5")
            candidate = json.loads((candidate_dir / "beta-policy-candidate.json").read_text(encoding="utf-8"))
            self.assertEqual(candidate["algorithmVersion"], "quarterly-risk-v5")

    def test_v2_sealed_activation_candidate_cannot_be_promoted(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            candidate = _sealed_cache({
                "dataCutoff": "2026-09-30",
                "algorithmVersion": "quarterly-risk-v2",
                "inputHash": "a" * 64,
                "assets": {},
            })
            (state_dir / "beta-policy-candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
            result = promote_beta_candidate_if_qualified(
                {"006208": 1_000_000}, 1_000_000, 0,
                market_by_symbol={"006208": "tw"}, state_dir=state_dir,
                today=date(2026, 10, 5),
            )
            self.assertEqual(result["status"], "NO_CURRENT_CANDIDATE")
            self.assertEqual(result["reasonCode"], "quarter_candidate_missing_or_algorithm_mismatch")

    def test_fund_bucket_is_not_queried_as_a_ticker_and_stays_unmodeled(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, audit_dir = Path(directory) / "state", Path(directory) / "audit"
            calls = []

            def fetcher(symbol, *, market, start, end, token=None):
                calls.append((symbol, market))
                return _research_series(symbol, market, start, end)

            summary = ensure_quarterly_risk_policies(
                {"台股": {"006208": 100}, "美股": {}, "基金": {"FUND": 1}},
                state_dir=state_dir, candidate_dir=audit_dir,
                today=date(2026, 10, 5), fetcher=fetcher,
            )
            self.assertFalse(any(symbol == "FUND" for symbol, _ in calls))
            self.assertEqual(summary["beta"]["unmodeledSymbols"], ["FUND"])
            blocked = promote_beta_candidate_if_qualified(
                {"006208": 980_000, "FUND": 20_000}, 1_000_000, 0,
                market_by_symbol={"006208": "tw"}, state_dir=state_dir,
                today=date(2026, 10, 5),
            )
            self.assertEqual(blocked["status"], "WAITING_FOR_PORTFOLIO_QUALIFICATION")
            self.assertIn("FUND", blocked["missingSymbols"])

    def test_new_us_holding_fetches_fx_even_when_kelly_policy_is_current(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir, candidate_dir = root / "state", root / "audit"
            calls = []

            def fetcher(symbol, *, market, start, end, token=None):
                calls.append(symbol)
                return _research_series(symbol, market, start, end)

            common = {"台股": {"006208": 100, "TEST": 25}, "美股": {}, "基金": {}}
            ensure_quarterly_risk_policies(
                common, state_dir=state_dir, candidate_dir=candidate_dir,
                today=date(2026, 10, 5), fetcher=fetcher,
            )
            self.assertTrue((state_dir / "kelly-policy-active.json").exists())
            calls.clear()

            with_us = {**common, "美股": {"AAPL": 10}}
            ensure_quarterly_risk_policies(
                with_us, state_dir=state_dir, candidate_dir=candidate_dir,
                today=date(2026, 10, 5), fetcher=fetcher,
            )
            self.assertIn("AAPL", calls)
            self.assertIn("TWD=X", calls)

    def test_beta_promotion_rejects_tampered_candidate_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir, candidate_dir = root / "state", root / "audit"

            def fetcher(symbol, *, market, start, end, token=None):
                return _research_series(symbol, market, start, end)

            ensure_quarterly_risk_policies(
                {"台股": {"006208": 100, "TEST": 50}, "美股": {}, "基金": {}},
                state_dir=state_dir, candidate_dir=candidate_dir,
                today=date(2026, 10, 5), fetcher=fetcher,
            )
            candidate_path = state_dir / "beta-policy-candidate.json"
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            candidate["inputHash"] = "f" * 64
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            result = promote_beta_candidate_if_qualified(
                {"006208": 900_000, "TEST": 100_000}, 1_000_000, 0,
                market_by_symbol={"006208": "tw", "TEST": "tw"},
                state_dir=state_dir, today=date(2026, 10, 5),
            )
            self.assertEqual(result["status"], "NO_CURRENT_CANDIDATE")
            self.assertFalse((state_dir / "beta-policy-active.json").exists())

    def test_readonly_parameter_validation_uses_staged_state_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "trusted-state"
            state_dir.mkdir()
            marker = state_dir / "preserve.txt"
            marker.write_text("unchanged", encoding="utf-8")

            def fetcher(symbol, *, market, start, end, token=None):
                return _research_series(symbol, market, start, end)

            result = validate_snapshot_readonly(
                {
                    "inventory": {"台股": {"006208": 100, "TEST": 50}, "美股": {}, "基金": {}},
                    "assetValuesTwd": {"006208": 900_000, "TEST": 100_000},
                    "totalAsset": 1_000_000,
                    "totalDebt": 0,
                    "marketBySymbol": {"006208": "tw", "TEST": "tw"},
                },
                state_dir=state_dir,
                today=date(2026, 10, 7),
                now=datetime(2026, 10, 7, tzinfo=timezone.utc),
                fetcher=fetcher,
            )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["beta"]["formalStatus"], "READY")
            self.assertIsNotNone(result["beta"]["navBeta"])
            self.assertEqual(result["beta"]["coveragePct"], 100.0)
            self.assertEqual(result["kelly"]["status"], "AUTO_VALIDATED")
            self.assertIsNotNone(result["kelly"]["halfKellyLimit"])
            self.assertFalse((state_dir / "beta-policy-active.json").exists())
            self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")
            self.assertEqual(result["sideEffects"]["telegram"], False)


if __name__ == "__main__":
    unittest.main()
