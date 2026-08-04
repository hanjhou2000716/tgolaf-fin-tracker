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


if __name__ == "__main__":
    unittest.main()
