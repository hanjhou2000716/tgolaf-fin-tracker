import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from beta_policy import _canonical_hash, load_active_beta_policy, load_active_kelly_policy
from quarterly_risk_policy import (
    ensure_quarterly_risk_policies,
    inventory_symbol_markets,
    promote_beta_candidate_if_qualified,
)


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
    events = {"splits": {}, "dividends": {}}
    evidence = {
        "provider": "Yahoo Chart API",
        "eventMapPresent": True,
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


if __name__ == "__main__":
    unittest.main()
