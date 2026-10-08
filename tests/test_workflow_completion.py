import unittest

from workflow_completion import completion_errors


class WorkflowCompletionTests(unittest.TestCase):
    def test_all_required_stages_and_matching_notification_pass(self):
        env = {"BUILD_RESULT": "success", "DEPLOY_RESULT": "success", "PUBLICATION_RESULT": "success",
               "REQUIRE_NOTIFICATION": "true", "EXPECTED_DATE": "2026-10-08", "EXPECTED_WINDOW": "tw"}
        notification = {"status": "SENT", "notificationType": "settlement", "windowDate": "2026-10-08", "window": "tw"}
        health = {"healthStatus": "PASS", "reasonCodes": []}
        self.assertEqual(completion_errors(env, notification, health), [])

    def test_pages_success_does_not_hide_missing_notification(self):
        env = {"BUILD_RESULT": "success", "DEPLOY_RESULT": "success", "PUBLICATION_RESULT": "success",
               "REQUIRE_NOTIFICATION": "true", "EXPECTED_DATE": "2026-10-08", "EXPECTED_WINDOW": "tw"}
        self.assertIn("NOTIFICATION_RESULT_MISSING", completion_errors(env, None, {"healthStatus": "PASS"}))

    def test_out_of_window_manual_run_does_not_require_settlement_notification(self):
        env = {"BUILD_RESULT": "success", "DEPLOY_RESULT": "success", "PUBLICATION_RESULT": "success",
               "REQUIRE_NOTIFICATION": "false"}
        self.assertEqual(completion_errors(env), [])


if __name__ == "__main__":
    unittest.main()
