import unittest
from pathlib import Path


class GoalContractStaticTests(unittest.TestCase):
    def test_runtime_has_no_legacy_fixed_goal_probability(self):
        source = (Path(__file__).resolve().parents[1] / "runtime_extensions.py").read_text(encoding="utf-8")
        self.assertNotIn("months=60", source)
        self.assertNotIn("paths=250", source)
        self.assertNotIn("max(float(net_asset), 10_000_000)", source)

    def test_pipeline_legacy_progress_uses_backend_target(self):
        source = (Path(__file__).resolve().parents[1] / "dashboard_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("active_goal_meta", source)
        self.assertIn("targetTwdEquivalent", source)
        self.assertNotIn("net_asset / 10000000", source)
        self.assertNotIn("10,000,000 TWD", source)


if __name__ == "__main__":
    unittest.main()
