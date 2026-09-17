import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from beta_policy import (
    estimate_beta_policy,
    load_active_beta_policy,
    load_active_kelly_policy,
    normalize_research_price,
    validate_active_policy_document,
    validate_active_kelly_document,
    weekly_research_series,
)
from risk import remaining_beta_capacity
from risk import calculate_nav_beta


def _rows(values, start="2020-01-01"):
    year, month, day = (int(part) for part in start.split("-"))
    from datetime import timedelta
    anchor = date(year, month, day)
    return [{"date": (anchor + timedelta(days=index * 7)).isoformat(), "close": value} for index, value in enumerate(values)]


class BetaPolicyTests(unittest.TestCase):
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
            {"TEST": {"rows": asset * 40, "currency": "USD", "source": "test", "corporateActionStatus": "PASS", "market": "us"}},
            benchmark * 40,
            fx_rows=fx * 40,
            cutoff="2024-12-31",
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
