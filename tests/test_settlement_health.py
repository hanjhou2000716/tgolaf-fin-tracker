import unittest

from settlement_health import build_settlement_health


class SettlementHealthTests(unittest.TestCase):
    def test_healthy_private_snapshot_returns_only_nonfinancial_marker(self):
        result = build_settlement_health(
            {
                "status": "ok",
                "ledgerAudit": {"status": "OK"},
                "ingestionHealth": {"status": "READY"},
            },
            {"portfolio": {"risk": {
                "beta": {"status": "READY", "policyStatus": "READY", "validationStatus": "AUTO_VALIDATED", "marketQuotesFresh": True},
                "kelly": {"status": "READY", "approvalStatus": "AUTO_VALIDATED"},
            }}},
            window_date="2026-10-07", window="us",
        )
        self.assertEqual(result["healthStatus"], "PASS")
        self.assertEqual(result["windowDate"], "2026-10-07")
        self.assertNotIn("portfolio", result)
        self.assertNotIn("holdings", result)

    def test_reference_values_or_degraded_ledger_are_not_healthy(self):
        result = build_settlement_health(
            {"status": "ok", "ledgerAudit": {"status": "DEGRADED"}, "ingestionHealth": {"status": "READY"}},
            {"portfolio": {"risk": {
                "beta": {"status": "READY", "policyStatus": "READY", "validationStatus": "NOT_READY", "marketQuotesFresh": True},
                "kelly": {"status": "READY", "approvalStatus": "AUTO_VALIDATED"},
            }}},
        )
        self.assertEqual(result["healthStatus"], "UNHEALTHY")
        self.assertIn("BETA_POLICY_NOT_AUTO_VALIDATED", result["reasonCodes"])
        self.assertIn("LEDGER_AUDIT_NOT_OK", result["reasonCodes"])


if __name__ == "__main__":
    unittest.main()
