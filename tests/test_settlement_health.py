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
            {"status": "SENT", "notificationType": "settlement", "windowDate": "2026-10-07", "window": "us"},
            window_date="2026-10-07", window="us",
        )
        self.assertEqual(result["healthStatus"], "PASS")
        self.assertEqual(result["completionStatus"], "COMPLETE")
        self.assertEqual(result["settlementStatus"], "COMPLETE")
        self.assertEqual(result["windowDate"], "2026-10-07")
        self.assertNotIn("portfolio", result)
        self.assertNotIn("holdings", result)

    def test_risk_unhealthy_is_separate_from_settlement_completion(self):
        result = build_settlement_health(
            {
                "status": "ok",
                "ledgerAudit": {"status": "OK"},
                "ingestionHealth": {"status": "READY"},
            },
            {"portfolio": {"risk": {
                "beta": {"status": "READY", "policyStatus": "READY", "validationStatus": "NOT_READY", "marketQuotesFresh": True},
                "kelly": {"status": "READY", "approvalStatus": "NOT_READY"},
            }}},
            {"status": "SENT", "notificationType": "settlement", "windowDate": "2026-10-07", "window": "tw"},
            window_date="2026-10-07", window="tw",
        )
        self.assertEqual(result["executionStatus"], "COMPLETED")
        self.assertEqual(result["completionStatus"], "COMPLETE")
        self.assertEqual(result["riskStatus"], "UNHEALTHY")
        self.assertEqual(result["healthStatus"], "UNHEALTHY")
        self.assertIn("NAV_BETA_NOT_READY", result["riskReasonCodes"])

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

    def test_settlement_without_matching_telegram_receipt_is_unhealthy(self):
        result = build_settlement_health(
            {"status": "ok", "ledgerAudit": {"status": "OK"}, "ingestionHealth": {"status": "READY"}},
            {"portfolio": {"risk": {
                "beta": {"status": "READY", "policyStatus": "READY", "validationStatus": "AUTO_VALIDATED", "marketQuotesFresh": True},
                "kelly": {"status": "READY", "approvalStatus": "AUTO_VALIDATED"},
            }}},
            {"status": "FAILED", "notificationType": "settlement", "windowDate": "2026-10-07", "window": "us"},
            window_date="2026-10-07", window="us",
        )
        self.assertEqual(result["healthStatus"], "UNHEALTHY")
        self.assertIn("SETTLEMENT_NOTIFICATION_NOT_CONFIRMED", result["reasonCodes"])


if __name__ == "__main__":
    unittest.main()
