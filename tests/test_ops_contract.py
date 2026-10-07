import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OpsContractTests(unittest.TestCase):
    def test_build_and_deploy_are_split_by_permissions(self):
        workflow = (ROOT / ".github" / "workflows" / "cron.yml").read_text(encoding="utf-8")
        self.assertIn("jobs:", workflow)
        self.assertIn("build:", workflow)
        self.assertIn("deploy:", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn('cron: "20 22 * * 1-5"', workflow)
        self.assertIn('cron: "25 7 * * 1-5"', workflow)
        self.assertIn("Conditional fallback gate", workflow)
        self.assertIn("SKIP_ALREADY_SUCCEEDED", (ROOT / "schedule_gate.py").read_text(encoding="utf-8"))
        self.assertIn("contents: read", workflow)
        build_section = workflow.split("  deploy:", 1)[0]
        deploy_section = workflow.split("  deploy:", 1)[1]
        self.assertNotIn("contents: write", workflow)
        self.assertIn("actions: read", deploy_section)
        self.assertIn("contents: read", deploy_section)
        self.assertIn("pages: write", deploy_section)
        self.assertIn("id-token: write", deploy_section)
        self.assertIn("pages_publication.py publish-and-verify public-site", deploy_section)
        self.assertNotIn("actions/deploy-pages", deploy_section)
        self.assertIn("Checkout publication verifier", deploy_section)
        self.assertIn("name: pages-publication-deployment-summary", deploy_section)
        verify_section = workflow.split("  verify-publication:", 1)[1]
        self.assertIn("python pages_publication.py verify-live public-site", verify_section)
        self.assertIn("name: pages-publication-verification", verify_section)
        self.assertIn("pages_build_version", (ROOT / "pages_publication.py").read_text(encoding="utf-8"))
        self.assertIn("upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02", workflow)
        self.assertIn("name: nav-beta-audit", workflow)
        self.assertIn("name: kelly-quarterly-candidate", workflow)
        # Quarterly parameters are built inside the tracker only after it has
        # read the actual de-duplicated holdings. Restore happens beforehand;
        # successful state is then persisted for the next main run.
        self.assertIn("Restore latest validated quarterly risk policy", workflow)
        self.assertIn("name: quarterly-risk-policy-state", workflow)
        self.assertLess(
            workflow.index("Restore latest validated quarterly risk policy"),
            workflow.index("Run Tracker Script (Generate private snapshot and public Demo)"),
        )
        self.assertNotIn("Build private quarterly Beta and Kelly candidates", workflow)
        pipeline = (ROOT / "dashboard_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("ensure_quarterly_risk_policies", pipeline)
        self.assertIn("name: beta-policy-candidate", workflow)
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
