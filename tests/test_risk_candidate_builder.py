import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import build_risk_candidates


class RiskCandidateBuilderTests(unittest.TestCase):
    @staticmethod
    def _fixture_series(symbol, *, market, start, end, variant=0):
        first_friday = start + timedelta(days=(4 - start.weekday()) % 7)
        rows = []
        day = first_friday
        index = 0
        while day <= end:
            if symbol == "006208":
                close = 100 + index * (0.23 + variant * 0.01) + (index % 9) * 0.41
            elif symbol == "TWD=X":
                close = 31 + (index % 11) * 0.025
            else:
                close = 40 + index * (0.12 + variant * 0.01) + ((index * 3) % 13) * 0.17
            rows.append({
                "date": day.isoformat(),
                "close": close,
                "splitAdjustedClose": close,
                "totalReturnIndex": close,
            })
            day += timedelta(days=7)
            index += 1
        return {
            "symbol": symbol,
            "market": market,
            "currency": "USD" if market == "us" else "TWD",
            "rows": rows,
            "source": "deterministic regression fixture",
            "corporateActionStatus": "PASS",
        }

    def test_candidate_generation_is_repeatable_and_preserves_hashed_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = {"variant": 0}

            def fetch(symbol, *, market, start, end, token=None):
                return self._fixture_series(
                    symbol, market=market, start=start, end=end, variant=calls["variant"],
                )

            with patch.dict(os.environ, {"RISK_CANDIDATE_OUTPUT_DIR": directory}, clear=False), \
                 patch("build_risk_candidates.fetch_research_series", side_effect=fetch):
                self.assertEqual(build_risk_candidates.main(), 0)
                beta_versions = sorted(Path(directory).glob("beta-policy-candidate-*.json"))
                kelly_versions = sorted(Path(directory).glob("kelly-quarterly-candidate-*.json"))
                self.assertEqual(len(beta_versions), 1)
                self.assertEqual(len(kelly_versions), 1)
                beta_before = beta_versions[0].read_bytes()
                kelly_before = kelly_versions[0].read_bytes()
                beta_payload = json.loads(beta_before)
                kelly_payload = json.loads(kelly_before)
                self.assertEqual(beta_payload["effectiveFromQuarter"], "2026Q4")
                self.assertEqual(beta_payload["inputHash"][:16], beta_payload["candidateId"].split("-")[-1])
                self.assertEqual(kelly_payload["effectiveFromQuarter"], "2026Q4")

                self.assertEqual(build_risk_candidates.main(), 0)
                self.assertEqual(beta_versions[0].read_bytes(), beta_before)
                self.assertEqual(kelly_versions[0].read_bytes(), kelly_before)

                calls["variant"] = 1
                self.assertEqual(build_risk_candidates.main(), 0)
                self.assertEqual(len(list(Path(directory).glob("beta-policy-candidate-*.json"))), 2)
                self.assertEqual(len(list(Path(directory).glob("kelly-quarterly-candidate-*.json"))), 2)
                self.assertEqual(beta_versions[0].read_bytes(), beta_before)
                self.assertEqual(kelly_versions[0].read_bytes(), kelly_before)


if __name__ == "__main__":
    unittest.main()
