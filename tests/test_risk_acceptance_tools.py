import base64
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from risk_acceptance_report import finalize_report
from risk_acceptance_snapshot import _canonical_hash, load_latest_risk_inputs


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _ReadOnlySession:
    def __init__(self, rows):
        self.rows = rows
        self.methods = []

    def get(self, *args, **kwargs):
        self.methods.append("GET")
        return _Response(self.rows)

    def post(self, *args, **kwargs):
        self.methods.append("POST")
        raise AssertionError("acceptance must not write")


def _payload(risk_inputs=None):
    inventory = {
        "台股": {"006208": 100}, "美股": {}, "基金": {},
        "現金_TWD": {"TWD": 0}, "現金_USD": {"USD": 0},
        "質押負債": {"Current_Debt": 0, "History": []},
        "質押利率": {"Rate": 3.3, "History": []}, "擔保品": {},
    }
    result = {
        "generatedAt": "2026-10-13T01:00:00+00:00",
        "status": "ok",
        "inventory": inventory,
        "portfolio": {"totalAsset": 100, "totalDebt": 0, "inventory": inventory},
    }
    if risk_inputs is not None:
        result["riskValidationInputs"] = risk_inputs
    return result


class RiskAcceptanceToolTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "not-a-real-key",
            "SUPABASE_USER_ID": "00000000-0000-0000-0000-000000000001",
        }

    def test_latest_snapshot_loader_is_get_only_and_restores_exact_inputs(self):
        risk = {
            "schemaVersion": 1,
            "inventory": _payload()["inventory"],
            "assetValuesTwd": {"006208": 100},
            "totalAsset": 100,
            "totalDebt": 0,
            "marketBySymbol": {"006208": "tw"},
        }
        risk["inputHash"] = _canonical_hash(risk)
        session = _ReadOnlySession([{
            "generated_at": "2026-10-13T01:00:00+00:00",
            "payload": _payload(risk),
        }])
        with patch.dict(os.environ, self.environment, clear=True):
            result = load_latest_risk_inputs(session=session, now=datetime(2026, 10, 13, 2, tzinfo=timezone.utc))
        self.assertEqual(result["inputPrecision"], "exact-runtime-inputs-v1")
        self.assertEqual(result["assetValuesTwd"], {"006208": 100})
        self.assertEqual(session.methods, ["GET"])

    def test_legacy_snapshot_reads_top_level_inventory_and_treats_cash_as_zero_beta(self):
        payload = _payload()
        payload["portfolio"]["assetTree"] = {
            "label": "總資產", "value": 100, "kind": "root",
            "children": [{"label": "現金與基金", "value": 100, "kind": "group", "children": [
                {"label": "現金", "value": 100, "kind": "leaf"},
            ]}],
        }
        session = _ReadOnlySession([{"generated_at": "2026-10-13T01:00:00+00:00", "payload": payload}])
        with patch.dict(os.environ, self.environment, clear=True):
            result = load_latest_risk_inputs(session=session, now=datetime(2026, 10, 13, 2, tzinfo=timezone.utc))
        self.assertEqual(result["inventory"], payload["inventory"])
        self.assertEqual(result["assetValuesTwd"], {"CASH_TWD": 100})

    def test_latest_snapshot_rejects_corrupt_risk_input_hash(self):
        risk = {
            "schemaVersion": 1, "inventory": {"台股": {"006208": 100}},
            "assetValuesTwd": {"006208": 100}, "totalAsset": 100,
            "totalDebt": 0, "marketBySymbol": {"006208": "tw"}, "inputHash": "bad",
        }
        session = _ReadOnlySession([{
            "generated_at": "2026-10-13T01:00:00+00:00", "payload": _payload(risk),
        }])
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "RISK_INPUT_HASH_MISMATCH"):
                load_latest_risk_inputs(session=session, now=datetime(2026, 10, 13, 2, tzinfo=timezone.utc))

    def test_latest_snapshot_rejects_exact_inputs_that_do_not_reconcile_to_snapshot(self):
        risk = {
            "schemaVersion": 1, "inventory": _payload()["inventory"],
            "assetValuesTwd": {"006208": 99}, "totalAsset": 100,
            "totalDebt": 0, "marketBySymbol": {"006208": "tw"},
        }
        risk["inputHash"] = _canonical_hash(risk)
        session = _ReadOnlySession([{
            "generated_at": "2026-10-13T01:00:00+00:00", "payload": _payload(risk),
        }])
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "EXACT_RISK_POSITION_TOTAL_MISMATCH"):
                load_latest_risk_inputs(session=session, now=datetime(2026, 10, 13, 2, tzinfo=timezone.utc))

    def test_report_encrypts_private_values_and_public_summary_is_sanitized(self):
        key = bytes(range(32))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            encrypted = root / "encrypted.json"
            summary = root / "summary.json"
            report.write_text(json.dumps({"status": "BLOCKED", "privateAmount": 987654321}), encoding="utf-8")
            with patch.dict(os.environ, {"RISK_ACCEPTANCE_ENCRYPTION_KEY": base64.b64encode(key).decode()}):
                result = finalize_report(report, candidate_sha="a" * 40, encrypted_path=encrypted, summary_path=summary)
            raw = encrypted.read_bytes()
            payload = json.loads(raw)
            plaintext = AESGCM(key).decrypt(
                base64.b64decode(payload["nonce"]),
                base64.b64decode(payload["ciphertext"]),
                ("a" * 40).encode("ascii"),
            )
            self.assertIn(b"987654321", plaintext)
            self.assertNotIn(b"987654321", summary.read_bytes())
            self.assertEqual(result["evidenceHash"], hashlib.sha256(raw).hexdigest())


if __name__ == "__main__":
    unittest.main()
