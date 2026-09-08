import math
import unittest

from buy_hold_policy import classify_buyhold_drawdown


class BuyHoldDrawdownUiTests(unittest.TestCase):
    def test_negative_drawdown_is_green_down_state(self):
        self.assertEqual(classify_buyhold_drawdown(-0.9), "is-down")

    def test_positive_value_is_red_up_state(self):
        self.assertEqual(classify_buyhold_drawdown(0.2), "is-up")

    def test_zero_and_missing_values_are_neutral(self):
        for value in (0, None, "", "not-a-number", math.nan, math.inf):
            with self.subTest(value=value):
                self.assertEqual(classify_buyhold_drawdown(value), "is-flat")


if __name__ == "__main__":
    unittest.main()
