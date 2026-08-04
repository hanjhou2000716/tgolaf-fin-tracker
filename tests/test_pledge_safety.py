import unittest

from pledge_safety import pledge_safety_center


class PledgeSafetyTests(unittest.TestCase):
    def test_distances_and_topup_are_calculated(self):
        result = pledge_safety_center(2000, 1000, stress_decline=0.1)
        self.assertEqual(result["currentRatio"], 200)
        self.assertEqual(result["distanceToWarningDecline"], 10)
        self.assertEqual(result["distanceToCallDecline"], 25)
        self.assertEqual(result["suggestedTopUp"], 0)
        self.assertEqual(result["stressRatio"], 180)
        self.assertEqual(result["status"], "healthy")

    def test_low_ratio_requires_topup(self):
        result = pledge_safety_center(1400, 1000)
        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["suggestedTopUp"], 400)

    def test_collateral_discounts_are_applied(self):
        result = pledge_safety_center({"006208": 2000, "2330": 1000}, 1000, pledged_discounts={"006208": 0.1})
        self.assertEqual(result["currentRatio"], 280)


if __name__ == "__main__":
    unittest.main()
