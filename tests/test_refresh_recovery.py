import unittest

from refresh_recovery import inventory_has_positive_assets, validate_recovery_candidate


def inventory(*, shares=1, debt=0, rate=3.3):
    return {
        "台股": {"006208": shares}, "美股": {}, "基金": {},
        "現金_TWD": {"TWD": 0}, "現金_USD": {"USD": 0},
        "質押負債": {"Current_Debt": debt, "History": []},
        "質押利率": {"Rate": rate, "History": []}, "擔保品": {},
    }


class RefreshRecoveryTests(unittest.TestCase):
    def test_pledge_metadata_alone_is_not_an_asset(self):
        empty = inventory(shares=0, debt=1_870_000, rate=3.3)
        self.assertFalse(inventory_has_positive_assets(empty))
        result = validate_recovery_candidate(empty, 0, previous_total_asset=8_000_000)
        self.assertFalse(result["ready"])
        self.assertEqual(result["reasonCode"], "BLOCKED_ZERO_RESULT")

    def test_valid_candidate_is_ready(self):
        result = validate_recovery_candidate(inventory(shares=100), 100_000)
        self.assertTrue(result["ready"])
        self.assertIsNone(result["reasonCode"])

    def test_rejected_or_pending_rows_block_candidate(self):
        self.assertFalse(validate_recovery_candidate(inventory(), 100, rejected_rows=1)["ready"])
        self.assertFalse(validate_recovery_candidate(inventory(), 100, pending_rows=1)["ready"])

    def test_incomplete_quotes_block_candidate(self):
        result = validate_recovery_candidate(inventory(), 100, quotes_complete=False)
        self.assertFalse(result["ready"])
        self.assertEqual(result["reasonCode"], "BLOCKED_RECOVERY_INVALID")

    def test_negative_asset_blocks_candidate(self):
        result = validate_recovery_candidate(inventory(shares=-1), 100)
        self.assertFalse(result["ready"])
        self.assertEqual(result["reasonCode"], "BLOCKED_RECOVERY_INVALID")


if __name__ == "__main__":
    unittest.main()
