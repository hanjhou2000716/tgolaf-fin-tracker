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

    def test_every_pages_publish_entry_uses_the_uploader_output_id(self):
        workflow_root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        paths = [workflow_root / "cron.yml", workflow_root / "settlement-notification-recovery.yml"]
        publish_steps = []
        upload_steps = 0
        for path in paths:
            text = path.read_text(encoding="utf-8")
            upload_steps += text.count("uses: actions/upload-pages-artifact@")
            chunks = text.split("      - name: ")
            publish_steps.extend(
                chunk for chunk in chunks
                if "python pages_publication.py publish-and-verify public-site" in chunk
            )
            self.assertNotIn("/actions/artifacts?per_page=100", text)
        self.assertEqual(upload_steps, 3)
        self.assertEqual(len(publish_steps), 3)
        for step in publish_steps:
            self.assertIn("PAGES_ARTIFACT_ID: ${{ steps.pages_artifact.outputs.artifact_id }}", step)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("id: pages_artifact"), text.count("uses: actions/upload-pages-artifact@"))

    def test_republication_workflows_bind_source_attempt_and_fallback_reads_recovery_proof(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            ".github/workflows/cron.yml",
            ".github/workflows/settlement-notification-recovery.yml",
        ):
            with self.subTest(workflow=relative):
                text = (root / relative).read_text(encoding="utf-8")
                self.assertIn("--source-run-attempt", text)
                self.assertIn("--json headBranch,headSha,event,attempt,jobs", text)
        recovery = (root / ".github/workflows/settlement-notification-recovery.yml").read_text(encoding="utf-8")
        self.assertIn("RECOVERY_SOURCE_RUN_ATTEMPT: ${{ github.event.workflow_run.run_attempt }}", recovery)
        gate = (root / "schedule_gate.py").read_text(encoding="utf-8")
        self.assertIn('"publication-recovery-result"', gate)
        self.assertIn('"pages-publication-recovery-summary.json"', gate)


if __name__ == "__main__":
    unittest.main()
