import unittest

from workflow_completion import completion_errors


class WorkflowCompletionTests(unittest.TestCase):
    def test_all_required_stages_and_matching_notification_pass(self):
        env = {"BUILD_RESULT": "success", "DEPLOY_RESULT": "success", "PUBLICATION_RESULT": "success",
               "REQUIRE_NOTIFICATION": "true", "EXPECTED_DATE": "2026-10-08", "EXPECTED_WINDOW": "tw"}
        notification = {"status": "SENT", "notificationType": "settlement", "windowDate": "2026-10-08", "window": "tw"}
        health = {"healthStatus": "PASS", "completionStatus": "COMPLETE", "reasonCodes": []}
        self.assertEqual(completion_errors(env, notification, health), [])

    def test_pages_success_does_not_hide_missing_notification(self):
        env = {"BUILD_RESULT": "success", "DEPLOY_RESULT": "success", "PUBLICATION_RESULT": "success",
               "REQUIRE_NOTIFICATION": "true", "EXPECTED_DATE": "2026-10-08", "EXPECTED_WINDOW": "tw"}
        self.assertIn("NOTIFICATION_RESULT_MISSING", completion_errors(
            env, None, {"healthStatus": "PASS", "completionStatus": "COMPLETE"}
        ))

    def test_risk_degradation_does_not_fail_completed_settlement(self):
        env = {"BUILD_RESULT": "success", "DEPLOY_RESULT": "success", "PUBLICATION_RESULT": "success",
               "REQUIRE_NOTIFICATION": "true", "EXPECTED_DATE": "2026-10-08", "EXPECTED_WINDOW": "tw"}
        notification = {"status": "SENT", "notificationType": "settlement", "windowDate": "2026-10-08", "window": "tw"}
        health = {
            "healthStatus": "UNHEALTHY",
            "dataStatus": "UNHEALTHY",
            "completionStatus": "COMPLETE",
            "completionReasonCodes": [],
            "reasonCodes": ["NAV_BETA_NOT_READY", "KELLY_NOT_READY"],
        }
        self.assertEqual(completion_errors(env, notification, health), [])

    def test_unexpected_quarterly_policy_error_fails_after_other_stages(self):
        env = {"BUILD_RESULT": "success", "DEPLOY_RESULT": "success", "PUBLICATION_RESULT": "success",
               "REQUIRE_NOTIFICATION": "true", "EXPECTED_DATE": "2026-10-08", "EXPECTED_WINDOW": "tw"}
        notification = {"status": "SENT", "notificationType": "settlement", "windowDate": "2026-10-08", "window": "tw"}
        health = {"completionStatus": "COMPLETE"}
        summary = {"status": "UNAVAILABLE", "technicalStatus": "ERROR",
                   "reasonCode": "QUARTERLY_POLICY_RUNTIME_ERROR"}
        self.assertEqual(
            completion_errors(env, notification, health, summary),
            ["QUARTERLY_POLICY_RUNTIME_ERROR"],
        )

    def test_incomplete_notification_stage_still_fails(self):
        env = {"BUILD_RESULT": "success", "DEPLOY_RESULT": "success", "PUBLICATION_RESULT": "success",
               "REQUIRE_NOTIFICATION": "true"}
        health = {"completionStatus": "INCOMPLETE", "completionReasonCodes": ["SETTLEMENT_NOTIFICATION_NOT_CONFIRMED"]}
        self.assertIn("SETTLEMENT_STAGES_NOT_COMPLETE", completion_errors(env, {}, health))

    def test_out_of_window_manual_run_does_not_require_settlement_notification(self):
        env = {"BUILD_RESULT": "success", "DEPLOY_RESULT": "success", "PUBLICATION_RESULT": "success",
               "REQUIRE_NOTIFICATION": "false"}
        self.assertEqual(completion_errors(env), [])


if __name__ == "__main__":
    unittest.main()
