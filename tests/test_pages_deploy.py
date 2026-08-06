import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / ".github" / "scripts" / "deploy_pages.py"
SPEC = importlib.util.spec_from_file_location("deploy_pages", MODULE_PATH)
DEPLOY_PAGES = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(DEPLOY_PAGES)


class PagesDeploymentTests(unittest.TestCase):
    def test_build_version_is_unique_per_run_artifact(self):
        first = DEPLOY_PAGES.unique_build_version("owner/repo", "100", 123)
        second = DEPLOY_PAGES.unique_build_version("owner/repo", "101", 123)
        third = DEPLOY_PAGES.unique_build_version("owner/repo", "100", 124)
        self.assertEqual(len(first), 40)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)


if __name__ == "__main__":
    unittest.main()
