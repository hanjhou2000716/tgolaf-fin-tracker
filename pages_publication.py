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
import re
import sys
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlencode, urlsplit, urlunsplit
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


def _manifest_hash_is_valid(manifest: Mapping[str, Any]) -> bool:
    unsigned = dict(manifest)
    supplied = unsigned.pop("contentHash", None)
    return isinstance(supplied, str) and supplied == _sha256(_canonical_json(unsigned))


def _source_order(manifest: Mapping[str, Any]) -> tuple[int, int]:
    try:
        run_id = int(manifest.get("sourceRunId", manifest.get("runId", 0)))
        run_attempt = int(manifest.get("sourceRunAttempt", manifest.get("runAttempt", 0)))
    except (TypeError, ValueError):
        return 0, 0
    return run_id, run_attempt


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PublicationError("publication manifest contains an unsafe content path")
    pieces = value.split("/")
    if any(piece in {"", ".", ".."} for piece in pieces):
        raise PublicationError("publication manifest contains an unsafe content path")
    return value


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
    source_commit = (env.get("PUBLICATION_SOURCE_COMMIT") or env.get("GITHUB_SHA", "")).strip()
    source_run_id = (env.get("PUBLICATION_SOURCE_RUN_ID") or run_id).strip()
    source_run_attempt = (env.get("PUBLICATION_SOURCE_RUN_ATTEMPT") or run_attempt).strip()
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
        "sourceRunId": source_run_id,
        "sourceRunAttempt": source_run_attempt,
        "sourceCommit": source_commit,
        "windowDate": env.get("PUBLICATION_WINDOW_DATE") or _service_value(status, "windowDate"),
        "window": env.get("PUBLICATION_WINDOW") or _service_value(status, "window"),
        "generatedAt": generated_at,
        "files": digests,
    }
    if env.get("PUBLICATION_RECOVERY_FROM"):
        body["republicationOf"] = env["PUBLICATION_RECOVERY_FROM"]
    body["contentHash"] = _sha256(_canonical_json(body))
    (root / MANIFEST_NAME).write_bytes(_canonical_json(body) + b"\n")
    return body


def prepare_republication(
    site_dir: str | Path,
    *,
    source_run_id: str,
    source_commit: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate a trusted failed run's public artifact, then issue a new publication identity."""
    env = dict(env or os.environ)
    root = Path(site_dir).resolve()
    try:
        original = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublicationError("source public artifact has no valid publication manifest") from error
    if not isinstance(original, Mapping) or not _manifest_hash_is_valid(original):
        raise PublicationError("source publication manifest failed its integrity check")
    repository = env.get("GITHUB_REPOSITORY", "").strip()
    if original.get("repository") != repository:
        raise PublicationError("source publication belongs to a different repository")
    if str(original.get("runId", "")) != str(source_run_id):
        raise PublicationError("source publication run identity does not match the failed workflow")
    if original.get("sourceCommit") != source_commit:
        raise PublicationError("source publication commit does not match the failed workflow")
    files = original.get("files")
    if not isinstance(files, Mapping) or not {"index.html", "status.json"}.issubset(files):
        raise PublicationError("source publication manifest is missing required public files")
    for relative, expected in files.items():
        safe_relative = _safe_relative_path(relative)
        path = (root / safe_relative).resolve()
        if root not in path.parents or not path.is_file() or _sha256(path.read_bytes()) != expected:
            raise PublicationError(f"source public artifact failed content-integrity check: {safe_relative}")

    if original.get("window") not in {None, "", "us", "tw"}:
        raise PublicationError("source publication window is invalid")
    if original.get("windowDate") not in {None, ""}:
        try:
            datetime.fromisoformat(str(original["windowDate"]))
        except ValueError as error:
            raise PublicationError("source publication date is invalid") from error
    env["PUBLICATION_SOURCE_RUN_ID"] = str(original.get("sourceRunId") or original["runId"])
    env["PUBLICATION_SOURCE_RUN_ATTEMPT"] = str(original.get("sourceRunAttempt") or original.get("runAttempt") or "1")
    env["PUBLICATION_SOURCE_COMMIT"] = source_commit
    env["PUBLICATION_WINDOW"] = str(original.get("window") or "")
    env["PUBLICATION_WINDOW_DATE"] = str(original.get("windowDate") or "")
    env["PUBLICATION_RECOVERY_FROM"] = str(original.get("publicationId", ""))
    env["PUBLICATION_REQUIRE_ORDERING_PROOF"] = "true"
    return prepare_manifest(root, env=env)


def pages_build_version(source_commit: str) -> str:
    """Use the source revision accepted by GitHub Pages' deployment endpoint.

    The deployment endpoint rejects synthetic per-run hashes with HTTP 404.
    Per-publication uniqueness is tracked by the public manifest and content
    hashes; live readback verifies those values after deployment.
    """
    value = str(source_commit or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise PublicationError("Pages build version must be the 40-character source commit SHA")
    return value


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
    except HTTPError as error:
        raw_error = error.read(4096).decode("utf-8", errors="replace")
        try:
            parsed_error = json.loads(raw_error)
        except (TypeError, ValueError):
            parsed_error = {}
        detail = parsed_error.get("message") if isinstance(parsed_error, Mapping) else None
        if not isinstance(detail, str) or not detail.strip():
            detail = "GitHub returned no safe error message"
        detail = re.sub(r"(?i)(Bearer\s+)[^\s,;]+", r"\1[REDACTED]", detail)
        detail = re.sub(r"gh[pousr]_[A-Za-z0-9_]+", "[REDACTED]", detail)
        detail = re.sub(r"github_pat_[A-Za-z0-9_]+", "[REDACTED]", detail)
        request_id = error.headers.get("X-GitHub-Request-Id", "") if error.headers else ""
        request_id = re.sub(r"[^A-Za-z0-9-]", "", request_id)[:80]
        suffix = f" requestId={request_id}" if request_id else ""
        raise PublicationError(
            f"GitHub Pages API {method} rejected request: HTTP {error.code}; {detail[:300]}{suffix}"
        ) from error
    except (URLError, TimeoutError) as error:
        raise PublicationError(f"GitHub Pages API {method} transport failed: {type(error).__name__}") from error
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
    # Match actions/deploy-pages: use the runner-issued OIDC request URL
    # verbatim and let the runner's configured audience policy apply.
    payload = _read_json_response(request_url, headers={"Authorization": f"Bearer {request_token}"})
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


def _publish_pages(repo: str, artifact_id: int, source_commit: str, attempt: int, oidc: str, token: str) -> tuple[str, str | None]:
    payload = _request_json(
        "POST", f"{API_ROOT}/repos/{repo}/pages/deployments", token=token,
        body={
            "artifact_id": artifact_id,
            "pages_build_version": pages_build_version(source_commit),
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
    _safe_relative_path(relative)
    safe_path = "/".join(quote(piece, safe="") for piece in relative.split("/"))
    url = urljoin(base_url.rstrip("/") + "/", safe_path)
    split = urlsplit(url)
    url = urlunsplit((split.scheme, split.netloc, split.path, urlencode({"publication_check": nonce}), ""))
    return reader(url, headers={"Cache-Control": "no-cache, no-store, max-age=0", "Pragma": "no-cache"})


def _verify_live_manifest_files(
    manifest: Mapping[str, Any], base_url: str, nonce: str, reader: Callable[..., Any]
) -> bool:
    files = manifest.get("files")
    if not isinstance(files, Mapping) or not files or not _manifest_hash_is_valid(manifest):
        return False
    for relative, expected in files.items():
        try:
            value = _live_payload(base_url, relative, nonce, reader)
        except Exception:
            return False
        if isinstance(value, str):
            value = value.encode("utf-8")
        if not isinstance(value, bytes) or _sha256(value) != expected:
            return False
    return True


def _publication_preflight(
    site_dir: str | Path,
    base_url: str,
    *,
    reader: Callable[..., Any] = _read_bytes_response,
) -> str:
    """Prevent delayed/older artifacts from replacing a newer live publication."""
    manifest = json.loads((Path(site_dir) / MANIFEST_NAME).read_text(encoding="utf-8"))
    nonce = _sha256(f"preflight:{manifest.get('publicationId')}:{time.monotonic()}".encode())[:16]
    try:
        raw = _live_payload(base_url, MANIFEST_NAME, nonce, reader)
    except HTTPError as error:
        if error.code == 404:
            return "NO_EXISTING_PUBLICATION"
        raise PublicationError(f"existing Pages manifest read failed: HTTP {error.code}") from error
    except Exception as error:
        raise PublicationError(f"existing Pages manifest read failed: {type(error).__name__}") from error
    try:
        live = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
    except (TypeError, ValueError) as error:
        raise PublicationError("existing Pages manifest is malformed; refusing to overwrite") from error
    if not isinstance(live, Mapping) or live.get("repository") != manifest.get("repository"):
        raise PublicationError("existing Pages manifest is unverifiable; refusing to overwrite")
    if not _manifest_hash_is_valid(live):
        raise PublicationError("existing Pages manifest hash is invalid; refusing to overwrite")
    candidate_order = _source_order(manifest)
    live_order = _source_order(live)
    if live_order > candidate_order:
        if not _verify_live_manifest_files(live, base_url, nonce, reader):
            raise PublicationError("newer Pages publication failed content-integrity check")
        return "SUPERSEDED"
    if live_order == candidate_order:
        if live.get("files") == manifest.get("files"):
            if _verify_live_manifest_files(live, base_url, nonce, reader):
                return "ALREADY_VISIBLE"
            # Same source and expected files, but CDN content is partial: it is
            # safe to republish this exact artifact to repair visibility.
            return "READY"
        raise PublicationError("publications from the same source run have conflicting content")
    return "READY"


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
    if not _manifest_hash_is_valid(manifest):
        raise PublicationError("prepared publication manifest content hash is invalid")

    def verify_manifest_files(candidate: Mapping[str, Any], nonce: str) -> bool:
        return _verify_live_manifest_files(candidate, base_url, nonce, reader)

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
                if _source_order(live_manifest) > _source_order(manifest):
                    if live_manifest.get("repository") == manifest.get("repository") and verify_manifest_files(live_manifest, nonce):
                        return "SUPERSEDED"
                    raise PublicationError("newer Pages manifest failed its content-integrity check")
                if _source_order(live_manifest) == _source_order(manifest):
                    if live_manifest.get("files") == manifest.get("files") and verify_manifest_files(live_manifest, nonce):
                        return "SUPERSEDED"
                    raise PublicationError("same-source Pages publication has conflicting content")
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
    preflight = _publication_preflight(site_dir, base_url)
    if preflight == "SUPERSEDED":
        return {"status": "SUPERSEDED", "publicationId": publication_id, "reasonCode": "NEWER_SOURCE_ALREADY_PUBLISHED"}
    if preflight == "ALREADY_VISIBLE":
        return {"status": "VERIFIED", "publicationId": publication_id, "reasonCode": "IDENTICAL_SOURCE_ALREADY_VISIBLE"}
    if preflight == "NO_EXISTING_PUBLICATION" and manifest.get("republicationOf"):
        raise PublicationError("republication requires a verifiable current manifest; refusing a blind overwrite")
    artifact_id = _find_pages_artifact(repo, run_id, token)
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            if attempt > 1:
                preflight = _publication_preflight(site_dir, base_url)
                if preflight == "SUPERSEDED":
                    return {"status": "SUPERSEDED", "publicationId": publication_id,
                            "reasonCode": "NEWER_SOURCE_PUBLISHED_DURING_RETRY"}
                if preflight == "ALREADY_VISIBLE":
                    return {"status": "VERIFIED", "publicationId": publication_id,
                            "reasonCode": "IDENTICAL_SOURCE_BECAME_VISIBLE"}
            # Fetch a fresh short-lived OIDC token for the retry as the first
            # Pages request may have consumed most of the token lifetime.
            oidc = _oidc_token(env)
            source_commit = str(manifest.get("sourceCommit") or "")
            status_url, page_url = _publish_pages(repo, artifact_id, source_commit, attempt, oidc, token)
            _await_deployment(status_url, token, wait=wait, deadline=monotonic() + 600, monotonic=monotonic)
            result = verify_live_publication(
                site_dir, base_url, expected_publication_id=publication_id,
                wait=wait, monotonic=monotonic, max_wait_seconds=300, poll_seconds=15,
            )
            if result == "SUPERSEDED":
                return {"status": "SUPERSEDED", "publicationId": publication_id, "pagesBuildVersion": pages_build_version(source_commit)}
            output_path = env.get("GITHUB_OUTPUT")
            if output_path:
                with open(output_path, "a", encoding="utf-8") as output:
                    output.write(f"page_url={page_url or base_url}\n")
            return {"status": "VERIFIED", "publicationId": publication_id, "deploymentAttempt": attempt,
                    "artifactId": artifact_id, "pagesBuildVersion": pages_build_version(source_commit),
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
    republish = sub.add_parser("prepare-republication")
    republish.add_argument("site_dir")
    republish.add_argument("--source-run-id", required=True)
    republish.add_argument("--source-commit", required=True)
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
        if args.command == "prepare-republication":
            manifest = prepare_republication(
                args.site_dir, source_run_id=args.source_run_id, source_commit=args.source_commit,
            )
            print(json.dumps({
                "status": "REPUBLISH_PREPARED", "publicationId": manifest["publicationId"],
                "sourceRunId": manifest["sourceRunId"], "contentHash": manifest["contentHash"],
            }))
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
