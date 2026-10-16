import unittest

from stage_recovery_completion import build_summary


class StageRecoveryCompletionTests(unittest.TestCase):
    def test_confirmed_notification_only_recovery_completes(self):
        result = build_summary("notification_only", notification_status="SENT")
        self.assertEqual(result["completionStatus"], "COMPLETE")
        self.assertEqual(result["reasonCodes"], [])

    def test_known_upstream_block_is_recorded_without_technical_failure(self):
        result = build_summary("notification_only", notification_status="BLOCKED_UPSTREAM")
        self.assertEqual(result["completionStatus"], "INCOMPLETE")
        self.assertEqual(result["reasonCodes"], ["UPSTREAM_RECOVERY_BLOCKED"])

    def test_missing_stage_proof_remains_a_technical_failure(self):
        result = build_summary("notification_and_publication", notification_status="SENT", publication_status="FAILED")
        self.assertEqual(result["completionStatus"], "INCOMPLETE")
        self.assertIn("PUBLICATION_RECOVERY_NOT_CONFIRMED", result["reasonCodes"])


if __name__ == "__main__":
    unittest.main()
