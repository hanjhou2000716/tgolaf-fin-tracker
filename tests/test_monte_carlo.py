import unittest

from monte_carlo import goal_probability, simulate_wealth


class MonteCarloTests(unittest.TestCase):
    def test_seeded_quantiles_are_reproducible(self):
        one = simulate_wealth(initial=100, annual_return=.06, annual_volatility=.15, months=12, paths=100, seed=42)
        two = simulate_wealth(initial=100, annual_return=.06, annual_volatility=.15, months=12, paths=100, seed=42)
        self.assertEqual(one["quantiles"], two["quantiles"])

    def test_goal_probability_is_bounded(self):
        result = goal_probability(initial=100, target=110, annual_return=.08, annual_volatility=.1, months=12, paths=200, seed=1)
        self.assertGreaterEqual(result["probability"], 0)
        self.assertLessEqual(result["probability"], 1)
        self.assertIn("P50", result["quantiles"])


if __name__ == "__main__":
    unittest.main()
