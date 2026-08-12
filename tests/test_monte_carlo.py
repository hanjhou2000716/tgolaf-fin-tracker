import unittest

from monte_carlo import first_passage_success, goal_probability, simulate_wealth


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

    def test_first_passage_counts_hit_before_terminal_drop(self):
        self.assertTrue(first_passage_success([98, 101, 97], 100))
        self.assertFalse(first_passage_success([98, 99, 97], 100))

    def test_deadline_probability_is_daily_and_has_contract_metadata(self):
        result = goal_probability(initial=100, target=101, annual_return=.08, annual_volatility=.1, as_of="2026-08-12", target_date="2026-08-20", paths=2000, seed=7)
        self.assertEqual(result["horizonDays"], 8)
        self.assertEqual(result["simulationGranularity"], "daily")
        self.assertEqual(result["paths"], 2000)
        self.assertEqual(result["probabilityDefinition"], "hit_by_deadline")


if __name__ == "__main__":
    unittest.main()
