"""Unique GitHub Pages deployment and cache-busted publication verification.

The public manifest contains only public-file hashes and workflow identity.
Private portfolio snapshots and quarterly risk artifacts are never inspected
or copied by this module.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


API_ROOT = "https://api.github.com"
MANIFEST_NAME = "publication.json"
USER_AGENT = "growth-pages-publication-verifier/1"


class PublicationError(RuntimeError):
    pass


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _service_value(status: Mapping[str, Any], key: str) -> Any:
    service = status.get("service")
    if isinstance(service, Mapping) and service.get(key) not in (None, ""):
        return service[key]
    return status.get(key)


def prepare_manifest(site_dir: str | Path, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = env or os.environ
    root = Path(site_dir).resolve()
    if not root.is_dir():
        raise PublicationError("public site directory does not exist")
    repo = env.get("GITHUB_REPOSITORY", "").strip()
    run_id = env.get("GITHUB_RUN_ID", "").strip()
    run_attempt = env.get("GITHUB_RUN_ATTEMPT", "").strip()
    source_commit = env.get("GITHUB_SHA", "").strip()
    if not repo or not run_id or not run_attempt or not source_commit:
        raise PublicationError("workflow repository, run identity, or source commit is missing")

    digests: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file() and item.name != MANIFEST_NAME):
        relative = path.relative_to(root).as_posix()
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise PublicationError("unsafe public artifact path")
        digests[relative] = _sha256(path.read_bytes())
    if not {"index.html", "status.json"}.issubset(digests):
        raise PublicationError("public artifact is missing index.html or status.json")

    try:
        status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublicationError("public status.json is unavailable or malformed") from error
    if not isinstance(status, Mapping):
        raise PublicationError("public status.json must be an object")
    generated_at = _iso(_service_value(status, "generatedAt"))
    publication_id = f"{repo}:{run_id}:{run_attempt}:1"
    body = {
        "schemaVersion": 1,
        "publicationId": publication_id,
        "publicationAttempt": 1,
        "repository": repo,
        "runId": run_id,
        "runAttempt": run_attempt,
        "sourceCommit": source_commit,
        "windowDate": env.get("PUBLICATION_WINDOW_DATE") or _service_value(status, "windowDate"),
        "window": env.get("PUBLICATION_WINDOW") or _service_value(status, "window"),
        "generatedAt": generated_at,
        "files": digests,
    }
    body["contentHash"] = _sha256(_canonical_json(body))
    (root / MANIFEST_NAME).write_bytes(_canonical_json(body) + b"\n")
    return body


def pages_build_version(publication_id: str, deployment_attempt: int) -> str:
    """Pages requires a unique build version even when source SHA is unchanged."""
    return _sha256(f"{publication_id}:deployment:{deployment_attempt}".encode("utf-8"))


def _read_json_response(url: str, *, headers: Mapping[str, str] | None = None, timeout: float = 20) -> Any:
    request = Request(url, headers={"User-Agent": USER_AGENT, **dict(headers or {})})
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read()
    except (HTTPError, URLError, TimeoutError) as error:
        raise PublicationError(f"request to {urlsplit(url).netloc} failed: {type(error).__name__}") from error
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationError(f"invalid JSON response from {urlsplit(url).netloc}") from error


def _read_bytes_response(url: str, *, headers: Mapping[str, str] | None = None, timeout: float = 20) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT, **dict(headers or {})})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def _request_json(method: str, url: str, *, token: str, body: Mapping[str, Any] | None = None) -> Any:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    request = Request(url, data=_canonical_json(body) if body is not None else None, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
    except (HTTPError, URLError, TimeoutError) as error:
        raise PublicationError(f"GitHub Pages API {method} failed: {type(error).__name__}") from error
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationError("GitHub Pages API returned invalid JSON") from error


def _oidc_token(env: Mapping[str, str] | None = None) -> str:
    env = env or os.environ
    request_url = env.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    request_token = env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not request_url or not request_token:
        raise PublicationError("GitHub Actions OIDC endpoint is unavailable")
    separator = "&" if "?" in request_url else "?"
    url = request_url + separator + urlencode({"audience": f"https://github.com/{env.get('GITHUB_REPOSITORY', '')}"})
    payload = _read_json_response(url, headers={"Authorization": f"Bearer {request_token}"})
    token = payload.get("value") if isinstance(payload, Mapping) else None
    if not isinstance(token, str) or not token:
        raise PublicationError("GitHub Actions OIDC token response is empty")
    return token


def _find_pages_artifact(repo: str, run_id: str, token: str) -> int:
    payload = _request_json("GET", f"{API_ROOT}/repos/{repo}/actions/artifacts?per_page=100", token=token)
    artifacts = payload.get("artifacts") if isinstance(payload, Mapping) else None
    matches = [
        item for item in artifacts or []
        if isinstance(item, Mapping)
        and item.get("name") == "github-pages"
        and str((item.get("workflow_run") or {}).get("id", "")) == str(run_id)
        and item.get("expired") is not True
    ]
    if not matches:
        raise PublicationError("current workflow has no non-expired github-pages artifact")
    matches.sort(key=lambda item: int(item.get("id", 0)), reverse=True)
    return int(matches[0]["id"])


def _publish_pages(repo: str, artifact_id: int, publication_id: str, attempt: int, oidc: str, token: str) -> tuple[str, str | None]:
    payload = _request_json(
        "POST", f"{API_ROOT}/repos/{repo}/pages/deployments", token=token,
        body={
            "artifact_id": artifact_id,
            "environment": "github-pages",
            "pages_build_version": pages_build_version(publication_id, attempt),
            "oidc_token": oidc,
        },
    )
    if not isinstance(payload, Mapping) or not payload.get("status_url"):
        raise PublicationError("GitHub accepted no trackable Pages deployment")
    page_url = payload.get("page_url") if isinstance(payload.get("page_url"), str) else None
    return str(payload["status_url"]), page_url


def _await_deployment(status_url: str, token: str, *, wait: Callable[[float], None], deadline: float, monotonic: Callable[[], float]) -> None:
    while monotonic() < deadline:
        payload = _request_json("GET", status_url, token=token)
        state = str(payload.get("status", "")).lower() if isinstance(payload, Mapping) else ""
        if state in {"succeed", "success", "succeeded"}:
            return
        if state in {"error", "failure", "failed"}:
            raise PublicationError("GitHub Pages deployment reported failure")
        wait(5)
    raise PublicationError("GitHub Pages deployment did not complete before its deadline")


def _live_payload(base_url: str, relative: str, nonce: str, reader: Callable[..., Any]) -> Any:
    safe_path = "/".join(quote(piece, safe="") for piece in relative.split("/"))
    url = urljoin(base_url.rstrip("/") + "/", safe_path)
    split = urlsplit(url)
    url = urlunsplit((split.scheme, split.netloc, split.path, urlencode({"publication_check": nonce}), ""))
    return reader(url, headers={"Cache-Control": "no-cache, no-store, max-age=0", "Pragma": "no-cache"})


def verify_live_publication(
    site_dir: str | Path,
    base_url: str,
    *,
    expected_publication_id: str | None = None,
    reader: Callable[..., Any] = _read_bytes_response,
    wait: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    max_wait_seconds: float = 300,
    poll_seconds: float = 15,
) -> str:
    root = Path(site_dir)
    try:
        manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublicationError("prepared publication manifest is missing or invalid") from error
    publication_id = manifest.get("publicationId") if isinstance(manifest, Mapping) else None
    if not isinstance(publication_id, str) or (expected_publication_id and publication_id != expected_publication_id):
        raise PublicationError("prepared publication identity does not match the active run")
    digest_map = manifest.get("files") if isinstance(manifest, Mapping) else None
    if not isinstance(digest_map, Mapping) or not digest_map:
        raise PublicationError("publication file hash manifest is empty")
    unsigned_manifest = dict(manifest)
    supplied_content_hash = unsigned_manifest.pop("contentHash", None)
    if supplied_content_hash != _sha256(_canonical_json(unsigned_manifest)):
        raise PublicationError("prepared publication manifest content hash is invalid")

    def verify_manifest_files(candidate: Mapping[str, Any], nonce: str) -> bool:
        files = candidate.get("files")
        if not isinstance(files, Mapping) or not files:
            return False
        unsigned = dict(candidate)
        supplied_content_hash = unsigned.pop("contentHash", None)
        if supplied_content_hash != _sha256(_canonical_json(unsigned)):
            return False
        for relative, expected in files.items():
            value = _live_payload(base_url, str(relative), nonce, reader)
            if isinstance(value, str):
                value = value.encode("utf-8")
            if not isinstance(value, bytes) or _sha256(value) != expected:
                return False
        return True

    deadline = monotonic() + max_wait_seconds
    last_error = "public content has not updated"
    while monotonic() <= deadline:
        nonce = hashlib.sha256(f"{publication_id}:{monotonic()}".encode()).hexdigest()[:16]
        try:
            live_manifest_payload = _live_payload(base_url, MANIFEST_NAME, nonce, reader)
            if isinstance(live_manifest_payload, bytes):
                live_manifest = json.loads(live_manifest_payload)
            elif isinstance(live_manifest_payload, str):
                live_manifest = json.loads(live_manifest_payload)
            else:
                live_manifest = live_manifest_payload
            if not isinstance(live_manifest, Mapping):
                raise PublicationError("live publication manifest is not an object")
            live_id = live_manifest.get("publicationId")
            if live_id != publication_id:
                def identity_order(value: Mapping[str, Any]) -> tuple[int, int]:
                    try:
                        return int(value.get("runId", 0)), int(value.get("runAttempt", 0))
                    except (TypeError, ValueError):
                        return 0, 0
                if identity_order(live_manifest) > identity_order(manifest):
                    if live_manifest.get("repository") == manifest.get("repository") and verify_manifest_files(live_manifest, nonce):
                        return "SUPERSEDED"
                    raise PublicationError("newer Pages manifest failed its content-integrity check")
                raise PublicationError("live Pages is still serving an older publication manifest")
            for relative, expected_hash in digest_map.items():
                live = _live_payload(base_url, str(relative), nonce, reader)
                if isinstance(live, str):
                    live = live.encode("utf-8")
                if not isinstance(live, bytes):
                    raise PublicationError(f"live {relative} response is not a byte/text payload")
                actual = _sha256(live)
                if actual != expected_hash:
                    raise PublicationError(f"live content hash mismatch for {relative}")
            return "VERIFIED"
        except PublicationError as error:
            last_error = str(error)
        except Exception as error:  # noqa: BLE001 - retry bounded cache/CDN visibility delays
            last_error = f"public readback failed: {type(error).__name__}"
        wait(min(poll_seconds, max(0, deadline - monotonic())))
    raise PublicationError(f"PUBLICATION_NOT_VISIBLE: {last_error}")


def publish_and_verify(site_dir: str | Path, *, env: Mapping[str, str] | None = None, wait: Callable[[float], None] = time.sleep,
                       monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    env = env or os.environ
    repo = env.get("GITHUB_REPOSITORY", "").strip()
    run_id = env.get("GITHUB_RUN_ID", "").strip()
    base_url = env.get("PAGES_BASE_URL", "").strip()
    token = env.get("GH_TOKEN", "")
    if not repo or not run_id or not base_url or not token:
        raise PublicationError("repository, run ID, Pages URL, or GitHub token is missing")
    manifest_path = Path(site_dir) / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    publication_id = str(manifest["publicationId"])
    artifact_id = _find_pages_artifact(repo, run_id, token)
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            # Fetch a fresh short-lived OIDC token for the retry as the first
            # Pages request may have consumed most of the token lifetime.
            oidc = _oidc_token(env)
            status_url, page_url = _publish_pages(repo, artifact_id, publication_id, attempt, oidc, token)
            _await_deployment(status_url, token, wait=wait, deadline=monotonic() + 600, monotonic=monotonic)
            result = verify_live_publication(
                site_dir, base_url, expected_publication_id=publication_id,
                wait=wait, monotonic=monotonic, max_wait_seconds=300, poll_seconds=15,
            )
            if result == "SUPERSEDED":
                return {"status": "SUPERSEDED", "publicationId": publication_id, "pagesBuildVersion": pages_build_version(publication_id, attempt)}
            output_path = env.get("GITHUB_OUTPUT")
            if output_path:
                with open(output_path, "a", encoding="utf-8") as output:
                    output.write(f"page_url={page_url or base_url}\n")
            return {"status": "VERIFIED", "publicationId": publication_id, "deploymentAttempt": attempt,
                    "artifactId": artifact_id, "pagesBuildVersion": pages_build_version(publication_id, attempt),
                    "pageUrl": page_url or base_url}
        except PublicationError as error:
            last_error = error
            if "SUPERSEDED" in str(error):
                return {"status": "SUPERSEDED", "publicationId": publication_id, "deploymentAttempt": attempt}
            if attempt == 2:
                break
    raise PublicationError(str(last_error or "PUBLICATION_NOT_VISIBLE"))


def _write_summary(result: Mapping[str, Any] | None, error: str | None, env: Mapping[str, str]) -> None:
    summary_path = Path(env.get("PUBLICATION_SUMMARY_PATH", ".private-build/pages-publication-summary.json"))
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary = dict(result or {})
    summary.update({
        "schemaVersion": 1,
        "status": summary.get("status") or ("FAILED" if error else "UNKNOWN"),
        "reasonCode": "PUBLICATION_NOT_VISIBLE" if error and "PUBLICATION_NOT_VISIBLE" in error else "PUBLICATION_FAILED" if error else None,
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
        "error": error,
    })
    summary_path.write_bytes(_canonical_json(summary) + b"\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("site_dir")
    deploy = sub.add_parser("publish-and-verify")
    deploy.add_argument("site_dir")
    verify = sub.add_parser("verify-live")
    verify.add_argument("site_dir")
    verify.add_argument("--base-url", default=os.getenv("PAGES_BASE_URL", ""))
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            manifest = prepare_manifest(args.site_dir)
            print(json.dumps({"status": "PREPARED", "publicationId": manifest["publicationId"], "contentHash": manifest["contentHash"]}))
            return 0
        if args.command == "verify-live":
            if not args.base_url:
                raise PublicationError("Pages base URL is required for independent verification")
            result = verify_live_publication(args.site_dir, args.base_url)
            manifest = json.loads((Path(args.site_dir) / MANIFEST_NAME).read_text(encoding="utf-8"))
            value = {"status": result, "publicationId": manifest.get("publicationId")}
            _write_summary(value, None, os.environ)
            print(json.dumps(value, sort_keys=True))
            return 0 if result in {"VERIFIED", "SUPERSEDED"} else 1
        result = publish_and_verify(args.site_dir)
        _write_summary(result, None, os.environ)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("status") in {"VERIFIED", "SUPERSEDED"} else 1
    except Exception as error:  # noqa: BLE001 - persist a private summary on deployment failure
        message = str(error)
        _write_summary(None, message, os.environ)
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
