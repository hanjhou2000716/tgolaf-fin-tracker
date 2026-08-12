import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import date
from decimal import Decimal

from supabase_sync import load_goal_state, save_goal_state, upload_private_snapshot, upload_private_transactions
from transaction_schema import Action, Transaction


class FakeResponse:
    def __init__(self, payload=None):
        self.payload = payload or []

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, existing=None):
        self.calls = []
        self.existing = existing or []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResponse(self.existing)

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResponse()


def sample_transaction(quantity="1"):
    return Transaction(
        transaction_id="55555555-5555-4555-8555-555555555555",
        source_row_id="Form:2",
        submitted_at="2026-08-04T00:00:00Z",
        submitter_email="owner@example.com",
        approved=True,
        transaction_date=date(2026, 8, 4),
        asset_type="TW_STOCK",
        symbol="006208",
        action=Action.BUY,
        quantity=Decimal(quantity),
        unit="SHARE",
        currency="TWD",
        price=Decimal("100"),
    )


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

    def test_transaction_sync_inserts_without_merge_duplicates(self):
        env = {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "server-only-key",
            "SUPABASE_USER_ID": "00000000-0000-0000-0000-000000000001",
            "SUPABASE_PRIVATE_SYNC_REQUIRED": "true",
        }
        fake_session = FakeSession()
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(upload_private_transactions([sample_transaction()], session=fake_session), "uploaded")
        get_url, _ = fake_session.calls[0]
        post_url, post_kwargs = fake_session.calls[1]
        self.assertEqual(get_url[0].split("/")[-1], "portfolio_transactions")
        self.assertIn("resolution=ignore-duplicates", post_kwargs["headers"]["Prefer"])
        self.assertEqual(post_kwargs["json"][0]["transaction_id"], sample_transaction().transaction_id)

    def test_transaction_conflict_fails_closed(self):
        env = {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "server-only-key",
            "SUPABASE_USER_ID": "00000000-0000-0000-0000-000000000001",
            "SUPABASE_PRIVATE_SYNC_REQUIRED": "true",
        }
        from ledger import transaction_payload
        existing = [{"transaction_id": sample_transaction().transaction_id, "payload": transaction_payload(sample_transaction("2"))}]
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                upload_private_transactions([sample_transaction("1")], session=FakeSession(existing))

    def test_goal_state_uses_private_service_boundary(self):
        env = {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "server-only-key",
            "SUPABASE_USER_ID": "00000000-0000-0000-0000-000000000001",
            "SUPABASE_PRIVATE_SYNC_REQUIRED": "true",
        }
        state = {"activeGoalId": "G1_TWD_10M", "status": "active", "achievements": []}
        fake_session = FakeSession(existing=[{"state": state}])
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(load_goal_state(session=fake_session), state)
            self.assertEqual(save_goal_state(state, session=fake_session), "uploaded")
        get_url, get_kwargs = fake_session.calls[0]
        post_url, post_kwargs = fake_session.calls[1]
        self.assertIn("goal_ladder_states", get_url[0])
        self.assertIn("goal_ladder_states", post_url[0])
        self.assertEqual(get_kwargs["headers"]["Authorization"], "Bearer server-only-key")
        self.assertEqual(post_kwargs["json"]["state"], state)


if __name__ == "__main__":
    unittest.main()
