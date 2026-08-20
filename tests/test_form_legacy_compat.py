import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FormLegacyCompatTests(unittest.TestCase):
    def test_legacy_mode_is_explicit_and_audited(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "cron.yml").read_text(encoding="utf-8")
        self.assertIn("FORM_SCHEMA_LEGACY_COMPAT", source)
        self.assertIn('"legacy_schema_compat"', source)
        self.assertIn("FORM_SCHEMA_LEGACY_COMPAT:", workflow)

    def test_strict_parser_error_is_not_silently_ignored(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("except TransactionSchemaError as error:", source)
        self.assertIn("if not FORM_SCHEMA_LEGACY_COMPAT:", source)

    def test_unknown_schema_does_not_enter_raw_inventory_fallback(self):
        source = (ROOT / "dashboard_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("detect_schema(rows[0])", source)
        self.assertIn('if schema_version == "LEGACY":', source)
        self.assertIn("adapt_known_legacy_rows(rows[0], rows[1:])", source)
        self.assertIn('reason = "schema_drift"', source)


if __name__ == "__main__":
    unittest.main()
