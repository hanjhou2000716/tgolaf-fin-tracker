import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from supabase_sync import _finite_json, upload_private_snapshot


class SupabaseFiniteJsonTests(unittest.TestCase):
    def test_nonfinite_values_become_null(self):
        value = _finite_json({"nan": float("nan"), "inf": float("inf"), "ok": 1.5, "items": [float("-inf")]})
        self.assertEqual(value, {"nan": None, "inf": None, "ok": 1.5, "items": [None]})

    def test_snapshot_post_body_is_json_safe(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps({
                "generatedAt": "2026-08-04T12:00:00+00:00",
                "status": "ok",
                "portfolio": {
                    "totalAsset": 1,
                    "inventory": {
                        "台股": {"006208": 1}, "美股": {}, "基金": {},
                        "現金_TWD": {"TWD": 0}, "現金_USD": {"USD": 0},
                        "質押負債": {"Current_Debt": 0, "History": []},
                        "質押利率": {"Rate": 3.3, "History": []}, "擔保品": {},
                    },
                },
                "value": float("nan"),
            }), encoding="utf-8")
            session = Mock()
            session.post.return_value.raise_for_status.return_value = None
            with patch.dict("os.environ", {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "server", "SUPABASE_USER_ID": "user"}, clear=True):
                upload_private_snapshot(str(path), session=session)
            body = session.post.call_args.kwargs["json"]
            self.assertIsNone(body["payload"]["value"])


if __name__ == "__main__":
    unittest.main()
