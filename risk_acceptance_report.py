"""Encrypt detailed private acceptance output and emit a safe public marker."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def finalize_report(report_path: str | Path, *, candidate_sha: str, encrypted_path: str | Path, summary_path: str | Path) -> dict[str, str]:
    key_text = os.getenv("RISK_ACCEPTANCE_ENCRYPTION_KEY", "").strip()
    try:
        key = base64.b64decode(key_text, validate=True)
    except (ValueError, TypeError) as error:
        raise RuntimeError("RISK_ACCEPTANCE_ENCRYPTION_KEY_INVALID") from error
    if len(key) != 32:
        raise RuntimeError("RISK_ACCEPTANCE_ENCRYPTION_KEY_INVALID")
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    nonce = os.urandom(12)
    aad = candidate_sha.encode("ascii")
    plaintext = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    encrypted = {
        "schemaVersion": 1,
        "algorithm": "AES-256-GCM",
        "candidateSha": candidate_sha,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    encrypted_bytes = (json.dumps(encrypted, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    encrypted_target = Path(encrypted_path)
    encrypted_target.parent.mkdir(parents=True, exist_ok=True)
    encrypted_target.write_bytes(encrypted_bytes)
    summary = {
        "schemaVersion": 1,
        "status": str(report.get("status", "BLOCKED")),
        "candidateSha": candidate_sha,
        "evidenceHash": hashlib.sha256(encrypted_bytes).hexdigest(),
    }
    summary_target = Path(summary_path)
    summary_target.parent.mkdir(parents=True, exist_ok=True)
    summary_target.write_text(json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--encrypted-output", required=True)
    parser.add_argument("--summary-output", required=True)
    args = parser.parse_args()
    summary = finalize_report(
        args.report,
        candidate_sha=args.candidate_sha,
        encrypted_path=args.encrypted_output,
        summary_path=args.summary_output,
    )
    Path(args.report).unlink()
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
