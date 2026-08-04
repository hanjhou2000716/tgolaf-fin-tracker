"""Upload the private snapshot to Supabase without exposing service credentials."""

import json
import os
from pathlib import Path

import requests


def _required_config():
    return {
        "url": os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
        "service_role_key": os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
        "user_id": os.getenv("SUPABASE_USER_ID", "").strip(),
    }


def upload_private_snapshot(path: str, *, session=None) -> str:
    """Upsert one private snapshot; return ``uploaded`` or ``skipped``.

    The default is non-blocking so the public Demo can still be built before
    Supabase secrets are configured. Set SUPABASE_PRIVATE_SYNC_REQUIRED=true
    in production to fail closed when the private sync is unavailable.
    """
    config = _required_config()
    configured = all(config.values())
    required = os.getenv("SUPABASE_PRIVATE_SYNC_REQUIRED", "false").lower() in {"1", "true", "yes", "on"}
    if not configured:
        if required:
            raise RuntimeError("Supabase private sync is required but SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, or SUPABASE_USER_ID is missing")
        print("Supabase private sync skipped; credentials are not configured")
        return "skipped"

    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    generated_at = snapshot.get("generatedAt")
    if not generated_at:
        raise ValueError("Private snapshot is missing generatedAt")
    body = {"user_id": config["user_id"], "generated_at": generated_at, "payload": snapshot}
    headers = {
        "apikey": config["service_role_key"],
        "Authorization": f"Bearer {config['service_role_key']}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    http = session or requests
    response = http.post(
        f"{config['url']}/rest/v1/portfolio_snapshots?on_conflict=user_id",
        headers=headers,
        json=body,
        timeout=20,
    )
    response.raise_for_status()
    print("Supabase private snapshot uploaded")
    return "uploaded"
