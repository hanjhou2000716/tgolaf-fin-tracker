import datetime as dt
import unittest
from pathlib import Path

from service_contracts import TAIPEI, UTC, parse_contract_timestamp, rfc3339_utc


ROOT = Path(__file__).resolve().parents[1]


class TimeContractTests(unittest.TestCase):
    def test_aware_timestamp_is_normalized_once_to_utc(self):
        value = parse_contract_timestamp("2026-09-13T16:14:00+08:00")
        self.assertEqual(value.astimezone(UTC).isoformat(), "2026-09-13T08:14:00+00:00")
        self.assertEqual(rfc3339_utc(value), "2026-09-13T08:14:00Z")

    def test_legacy_naive_timestamp_means_taipei_wall_time(self):
        value = parse_contract_timestamp("2026-09-13T16:14:00")
        self.assertEqual(value.tzinfo, TAIPEI)
        self.assertEqual(rfc3339_utc(value), "2026-09-13T08:14:00Z")

    def test_pipeline_does_not_construct_naive_utc_plus_eight(self):
        source = (ROOT / "dashboard_pipeline.py").read_text(encoding="utf-8")
        self.assertNotIn("datetime.datetime.utcnow() + datetime.timedelta(hours=8)", source)
        self.assertIn("datetime.datetime.now(UTC)", source)
        self.assertIn('"generatedAt": generated_at', source)


if __name__ == "__main__":
    unittest.main()
