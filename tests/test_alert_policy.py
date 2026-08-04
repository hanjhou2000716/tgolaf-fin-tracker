from datetime import datetime, timezone
import unittest

from alert_policy import evaluate_alert_policy
from alerts import AlertEngine


class AlertPolicyTests(unittest.TestCase):
    def test_stress_ratio_is_mapped_and_recovery_is_reported(self):
        clock = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)
        engine = AlertEngine(now=lambda: clock)
        first = evaluate_alert_policy({"stressRatio": 140, "maintenanceRatio": 170}, engine=engine, now=clock)
        self.assertTrue(any(item["key"] == "maintenance_critical" for item in first["active"]))
        recovered = evaluate_alert_policy({"stressRatio": 200, "maintenanceRatio": 200}, engine=engine, now=clock)
        self.assertTrue(any(item["key"] == "maintenance_critical" for item in recovered["recovered"]))

    def test_quote_age_becomes_stale_after_24_hours(self):
        now = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)
        result = evaluate_alert_policy({"quoteFetchedAt": "2026-08-03T11:00:00+00:00"}, now=now)
        self.assertTrue(any(item["key"] == "stale_warning" for item in result["active"]))


if __name__ == "__main__":
    unittest.main()
