from datetime import datetime, timezone
import unittest

from data_health import build_data_health


class DataHealthTests(unittest.TestCase):
    def test_stale_and_source_fallback_are_visible(self):
        report = build_data_health(last_sync="2026-08-03T00:00:00+00:00", now=datetime(2026, 8, 4, tzinfo=timezone.utc), sources={"Yahoo": {"quality": "stale", "fallback_used": True}}, pending_transactions=2)
        self.assertEqual(report["status"], "stale")
        self.assertTrue(report["sources"][0]["fallbackUsed"])
        self.assertEqual(report["pendingTransactions"], 2)

    def test_reconciliation_failure_is_critical(self):
        report = build_data_health(last_sync="2026-08-04T00:00:00+00:00", now=datetime(2026, 8, 4, 1, tzinfo=timezone.utc), sources={}, reconciled=False)
        self.assertEqual(report["status"], "critical")


if __name__ == "__main__":
    unittest.main()
