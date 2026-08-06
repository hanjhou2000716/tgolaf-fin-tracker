"""Deploy the workflow artifact to GitHub Pages with a unique build version.

GitHub's official deploy-pages action uses GITHUB_SHA as pages_build_version.
This repository refreshes data from cron without creating a source commit, so
multiple runs can otherwise submit the same Pages deployment and be cancelled.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from typing import Any
from urllib.parse import urlencode

import requests


API_VERSION = "2022-11-28"
POLL_INTERVAL_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 600


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
    }


def _json_response(response: requests.Response, operation: str) -> dict[str, Any]:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"{operation} returned a non-object response")
    return payload


def unique_build_version(repository: str, run_id: str, artifact_id: int) -> str:
    """Return a stable, unique Pages version for one workflow artifact."""

    seed = f"{repository}:{run_id}:{artifact_id}".encode("utf-8")
    # The Pages deployment endpoint accepts the same 40-character SHA format
    # used by GITHUB_SHA, while still requiring a value unique per artifact.
    return hashlib.sha1(seed).hexdigest()


def _get_artifact(session: requests.Session, api_url: str, repository: str, run_id: str, token: str) -> dict[str, Any]:
    artifact_name = os.environ.get("PAGES_ARTIFACT_NAME", "github-pages")
    url = f"{api_url}/repos/{repository}/actions/runs/{run_id}/artifacts"
    response = session.get(
        url,
        headers=_headers(token),
        params={"name": artifact_name, "per_page": 100},
        timeout=30,
    )
    payload = _json_response(response, "Pages artifact lookup")
    artifacts = [item for item in payload.get("artifacts", []) if not item.get("expired")]
    if len(artifacts) != 1:
        raise RuntimeError(
            f"Expected exactly one active Pages artifact named {artifact_name!r}; found {len(artifacts)}"
        )
    return artifacts[0]


def _get_oidc_token(session: requests.Session) -> str:
    url = _required_env("ACTIONS_ID_TOKEN_REQUEST_URL")
    token = _required_env("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    # Keep the default audience used by actions/deploy-pages.
    if "?" not in url:
        url = f"{url}?{urlencode({'api-version': '2.0'})}"
    response = session.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    payload = _json_response(response, "OIDC token request")
    value = str(payload.get("value", "")).strip()
    if not value:
        raise RuntimeError("OIDC token response did not contain a value")
    return value


def deploy() -> str:
    repository = _required_env("GITHUB_REPOSITORY")
    run_id = _required_env("GITHUB_RUN_ID")
    token = _required_env("GITHUB_TOKEN")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    timeout_seconds = int(os.environ.get("PAGES_DEPLOYMENT_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))

    session = requests.Session()
    artifact = _get_artifact(session, api_url, repository, run_id, token)
    artifact_id = int(artifact["id"])
    build_version = unique_build_version(repository, run_id, artifact_id)
    oidc_token = _get_oidc_token(session)

    deployment_url = f"{api_url}/repos/{repository}/pages/deployments"
    deployment = _json_response(
        session.post(
            deployment_url,
            headers={**_headers(token), "Content-Type": "application/json"},
            json={
                "artifact_id": artifact_id,
                "pages_build_version": build_version,
                "oidc_token": oidc_token,
            },
            timeout=30,
        ),
        "Pages deployment creation",
    )
    deployment_id = str(deployment.get("id") or build_version)
    status_url = f"{api_url}/repos/{repository}/pages/deployments/{deployment_id}"
    deadline = time.monotonic() + timeout_seconds
    final_states = {"succeed", "failure", "error", "deployment_failed", "deployment_cancelled", "deployment_content_failed"}

    while time.monotonic() < deadline:
        status = _json_response(
            session.get(status_url, headers=_headers(token), timeout=30),
            "Pages deployment status",
        )
        state = str(status.get("status", "")).lower()
        print(f"Pages deployment status: {state or 'unknown'}", flush=True)
        if state == "succeed":
            page_url = str(status.get("page_url") or deployment.get("page_url") or "").strip()
            output_path = os.environ.get("GITHUB_OUTPUT")
            if output_path and page_url:
                with open(output_path, "a", encoding="utf-8") as output:
                    output.write(f"page_url={page_url}\n")
            return page_url
        if state in final_states:
            raise RuntimeError(f"Pages deployment ended in {state}")
        time.sleep(POLL_INTERVAL_SECONDS)

    raise TimeoutError(f"Pages deployment did not complete within {timeout_seconds} seconds")


if __name__ == "__main__":
    try:
        page_url = deploy()
        if page_url:
            print(f"Pages deployed: {page_url}")
    except Exception as error:  # pragma: no cover - exercised by Actions
        print(f"Pages deployment failed: {error}", file=sys.stderr)
        raise
