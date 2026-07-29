import datetime
import unittest

from health_check import TAIPEI, evaluate_status, parse_generated_at


class HealthCheckTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(2026, 7, 29, 18, 0, tzinfo=TAIPEI)

    def test_accepts_fresh_healthy_contract(self):
        payload = {
            "status": "ok", "generatedAt": "2026-07-29T16:00:00+08:00",
            "freshness": {"staleAfterHours": 18}, "sources": {"googleSheet": "ok"},
        }
        self.assertEqual(evaluate_status("Growth", payload, self.now), [])

    def test_reports_degraded_stale_and_source_failure(self):
        payload = {
            "status": "degraded", "generatedAt": "2026-07-28T16:00:00+08:00",
            "staleAfterHours": 18, "sources": {"vix": "unavailable"},
        }
        issues = evaluate_status("Skynet", payload, self.now)
        self.assertEqual(len(issues), 3)
        self.assertIn("status=degraded", issues[0])
        self.assertIn("stale", issues[1])
        self.assertIn("source vix", issues[2])

    def test_interprets_legacy_growth_timestamp_as_taipei(self):
        parsed = parse_generated_at("2026-07-29T16:00:00")
        self.assertEqual(parsed.tzinfo, TAIPEI)
        self.assertEqual(parsed.hour, 16)


if __name__ == "__main__":
    unittest.main()
