import unittest

from advisor import build_advice


class AdvisorTests(unittest.TestCase):
    def test_card_contains_explanation_and_failed_guardrails(self):
        card = build_advice(action="補充現金", reason="壓力後維持率偏低", expected_improvement="提高安全邊界", side_effects="降低可投資現金", data_as_of="2026-08-04T12:00:00+08:00", confidence=.8, before={"ratio": 145}, after={"ratio": 180}, guardrails={"minimumCash": False, "staleData": False})
        self.assertEqual(card["failedGuardrails"], ["minimumCash", "staleData"])
        self.assertTrue(card["requiresApproval"])
        self.assertFalse(card["isTradeInstruction"])

    def test_confidence_bounds(self):
        with self.assertRaises(ValueError):
            build_advice(action="x", reason="y", expected_improvement="z", side_effects="q", data_as_of="now", confidence=2, before={}, after={})


if __name__ == "__main__":
    unittest.main()
