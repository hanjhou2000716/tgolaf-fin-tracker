from pathlib import Path
import unittest


class ArchitectureTests(unittest.TestCase):
    def test_main_is_a_small_compatibility_entrypoint(self):
        self.assertLessEqual(len(Path("main.py").read_text(encoding="utf-8").splitlines()), 20)
        self.assertTrue(Path("dashboard_pipeline.py").exists())


if __name__ == "__main__":
    unittest.main()
