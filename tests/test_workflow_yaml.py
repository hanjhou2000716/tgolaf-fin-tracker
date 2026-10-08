from pathlib import Path
import unittest

import yaml


class WorkflowYamlTests(unittest.TestCase):
    def test_all_github_workflows_parse(self):
        workflow_root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        paths = sorted((*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")))
        self.assertTrue(paths, "no workflow files were found")
        for path in paths:
            with self.subTest(workflow=path.name):
                with path.open("r", encoding="utf-8") as handle:
                    self.assertIsInstance(yaml.safe_load(handle), dict)


if __name__ == "__main__":
    unittest.main()
