import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from supabase_sync import upload_private_snapshot


class FakeResponse:
    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResponse()


class SupabaseSyncTests(unittest.TestCase):
    def test_missing_config_skips_without_private_upload(self):
        with patch.dict(os.environ, {}, clear=True):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "data.private.json"
                path.write_text(json.dumps({"generatedAt": "2026-08-04T12:00:00+08:00"}), encoding="utf-8")
                self.assertEqual(upload_private_snapshot(str(path)), "skipped")

    def test_required_config_fails_closed_when_missing(self):
        with patch.dict(os.environ, {"SUPABASE_PRIVATE_SYNC_REQUIRED": "true"}, clear=True):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "data.private.json"
                path.write_text(json.dumps({"generatedAt": "2026-08-04T12:00:00+08:00"}), encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    upload_private_snapshot(str(path))

    def test_upsert_uses_service_role_only_on_server_side(self):
        env = {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "server-only-key",
            "SUPABASE_USER_ID": "00000000-0000-0000-0000-000000000001",
            "SUPABASE_PRIVATE_SYNC_REQUIRED": "true",
        }
        fake_session = FakeSession()
        with patch.dict(os.environ, env, clear=True):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "data.private.json"
                path.write_text(json.dumps({"generatedAt": "2026-08-04T12:00:00+08:00", "portfolio": {"netAsset": 1}}), encoding="utf-8")
                self.assertEqual(upload_private_snapshot(str(path), session=fake_session), "uploaded")
        url, kwargs = fake_session.calls[0]
        self.assertIn("on_conflict=user_id", url[0])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer server-only-key")
        self.assertEqual(kwargs["json"]["user_id"], env["SUPABASE_USER_ID"])


if __name__ == "__main__":
    unittest.main()
