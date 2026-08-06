import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OpsContractTests(unittest.TestCase):
    def test_build_and_deploy_are_split_by_permissions(self):
        workflow = (ROOT / ".github" / "workflows" / "cron.yml").read_text(encoding="utf-8")
        self.assertIn("jobs:", workflow)
        self.assertIn("build:", workflow)
        self.assertIn("deploy:", workflow)
        self.assertIn("cancel-in-progress: true", workflow)
        self.assertIn('cron: "40 21 * * 1-5"', workflow)
        self.assertIn('cron: "45 6 * * 1-5"', workflow)
        self.assertIn("contents: read", workflow)
        build_section = workflow.split("  deploy:", 1)[0]
        deploy_section = workflow.split("  deploy:", 1)[1]
        self.assertNotIn("contents: write", workflow)
        self.assertIn("actions: read", deploy_section)
        self.assertIn("contents: read", deploy_section)
        self.assertIn("pages: write", deploy_section)
        self.assertIn("id-token: write", deploy_section)
        self.assertIn("actions/deploy-pages@cd2ce8fcbc39b97be8ca5fce6e763baed58fa128", deploy_section)
        self.assertNotIn("deploy_pages.py", deploy_section)
        self.assertIn("upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02", workflow)
        self.assertIn("touch public-site/.nojekyll", workflow)

    def test_actions_are_pinned_to_commit_shas(self):
        for path in (ROOT / ".github" / "workflows").glob("*.yml"):
            content = path.read_text(encoding="utf-8")
            for line in content.splitlines():
                if "uses:" in line:
                    reference = line.split("uses:", 1)[1].split("#", 1)[0].strip()
                    self.assertRegex(reference, r"@[0-9a-f]{40}$", f"Unpinned action in {path}: {line}")

    def test_dependency_lock_is_used(self):
        lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        for line in requirements.splitlines():
            if line and not line.startswith("#"):
                self.assertIn(line, lock)


if __name__ == "__main__":
    unittest.main()
