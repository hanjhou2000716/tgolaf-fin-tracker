import unittest
from datetime import datetime, timezone, timedelta

from alerts import AlertEngine


class AlertTests(unittest.TestCase):
    def test_trigger_cooldown_and_recovery(self):
        current = [datetime(2026, 8, 4, tzinfo=timezone.utc)]
        engine = AlertEngine(cooldown_minutes=60, now=lambda: current[0])
        bad = {"maintenanceRatio": 170}
        first = engine.evaluate(bad)
        self.assertTrue(next(item for item in first if item["key"] == "maintenance_warning")["send"])
        current[0] += timedelta(minutes=10)
        second = engine.evaluate(bad)
        self.assertFalse(next(item for item in second if item["key"] == "maintenance_warning")["send"])
        current[0] += timedelta(minutes=60)
        third = engine.evaluate(bad)
        self.assertTrue(next(item for item in third if item["key"] == "maintenance_warning")["send"])
        recovered = engine.evaluate({"maintenanceRatio": 200})
        item = next(item for item in recovered if item["key"] == "maintenance_warning")
        self.assertTrue(item["recovered"])
        self.assertTrue(item["send"])

    def test_acknowledgment_is_scoped_to_active_alert(self):
        engine = AlertEngine(now=lambda: datetime(2026, 8, 4, tzinfo=timezone.utc))
        engine.evaluate({"maxCompanyExposure": 40})
        self.assertTrue(engine.acknowledge("concentration_warning"))
        self.assertTrue(engine.states["concentration_warning"].acknowledged)
        self.assertFalse(engine.acknowledge("missing"))

    def test_ingestion_degraded_is_actionable_warning(self):
        engine = AlertEngine(now=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc))
        alerts = engine.evaluate({
            "ingestionDegraded": True,
            "ingestionMessage": "IMMUTABLE_LEDGER_CONFLICT",
        })
        item = next(alert for alert in alerts if alert["key"] == "ingestion_warning")
        self.assertTrue(item["triggered"])
        self.assertEqual(item["message"], "IMMUTABLE_LEDGER_CONFLICT")


if __name__ == "__main__":
    unittest.main()
