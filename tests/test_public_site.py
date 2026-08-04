import json
import re
import tempfile
import unittest
from pathlib import Path

from public_site import build_public_payload, build_public_status, write_public_site


class PublicSiteSecurityTests(unittest.TestCase):
    def test_public_contract_is_demo_only(self):
        payload = build_public_payload("2026-08-04T12:00:00+08:00")
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["mode"], "demo")
        self.assertIn("allocation", payload["portfolio"])
        self.assertNotIn("totalAsset", encoded)
        self.assertNotIn("netAsset", encoded)
        self.assertNotIn("assetTree", encoded)
        self.assertNotIn("006208", encoded)
        self.assertNotIn("QQQM", encoded)

    def test_public_status_has_no_portfolio_values(self):
        status = build_public_status("2026-08-04T12:00:00+08:00")
        encoded = json.dumps(status, ensure_ascii=False)
        self.assertEqual(status["mode"], "demo")
        self.assertNotIn("portfolio", encoded)
        self.assertNotIn("totalAsset", encoded)

    def test_written_site_contains_only_safe_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            write_public_site(directory, "2026-08-04T12:00:00+08:00")
            files = {path.name for path in Path(directory).iterdir()}
            self.assertEqual(files, {"index.html", "data.public.json", "status.json", "private"})
            html = (Path(directory) / "index.html").read_text(encoding="utf-8")
            data = (Path(directory) / "data.public.json").read_text(encoding="utf-8")
            private_html = (Path(directory) / "private" / "index.html").read_text(encoding="utf-8")
            for content in (html, data):
                self.assertNotIn("006208", content)
                self.assertNotIn("QQQM", content)
                self.assertNotIn("assetTree", content)
                self.assertIsNone(re.search(r"NT\$[0-9]", content))
            self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", private_html)
            self.assertNotIn("server-only-key", private_html)


if __name__ == "__main__":
    unittest.main()
