import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from urllib.error import HTTPError

from pages_publication import (
    PublicationError,
    _publication_preflight,
    pages_build_version,
    prepare_manifest,
    prepare_republication,
    verify_live_publication,
    _request_json,
    _oidc_token,
    _publish_pages,
)


class _Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def wait(self, seconds):
        self.value += max(seconds, 0.001)


class PagesPublicationTests(unittest.TestCase):
    def test_oidc_uses_runner_request_url_without_rewriting_audience(self):
        request_url = "https://token.actions.githubusercontent.com/idtoken?audience=runner-default"
        with patch("pages_publication._read_json_response", return_value={"value": "opaque-token"}) as read:
            result = _oidc_token({
                "ACTIONS_ID_TOKEN_REQUEST_URL": request_url,
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-secret",
                "GITHUB_REPOSITORY": "owner/repo",
            })
        self.assertEqual(result, "opaque-token")
        self.assertEqual(read.call_args.args[0], request_url)

    def test_pages_request_uses_source_commit_build_version(self):
        source_commit = "a" * 40
        with patch("pages_publication._request_json", return_value={"status_url": "https://api.github.test/status"}) as request:
            _publish_pages("owner/repo", 77, source_commit, 1, "oidc", "token")
        method, url = request.call_args.args
        body = request.call_args.kwargs["body"]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/pages/deployments"))
        self.assertEqual(set(body), {"artifact_id", "pages_build_version", "oidc_token"})
        self.assertEqual(body["pages_build_version"], source_commit)
        self.assertEqual(pages_build_version(source_commit), source_commit)

    def test_pages_build_version_rejects_synthetic_or_malformed_values(self):
        with self.assertRaisesRegex(PublicationError, "source commit SHA"):
            pages_build_version("synthetic-publication-hash")
        with self.assertRaisesRegex(PublicationError, "source commit SHA"):
            pages_build_version("a" * 64)

    def test_pages_http_error_keeps_safe_status_and_request_id(self):
        error = HTTPError(
            "https://api.github.com/repos/owner/repo/pages/deployments",
            422,
            "Validation Failed",
            {"X-GitHub-Request-Id": "REQ-123"},
            io.BytesIO(b'{"message":"invalid token Bearer ghp_secretvalue"}'),
        )
        with patch("pages_publication.urlopen", side_effect=error):
            with self.assertRaisesRegex(PublicationError, "HTTP 422") as caught:
                _request_json("POST", "https://api.github.com/ignored", token="ghp_secretvalue", body={})
        self.assertIn("REQ-123", str(caught.exception))
        self.assertNotIn("ghp_secretvalue", str(caught.exception))

    def _site(self, root):
        root.mkdir()
        (root / ".nojekyll").write_bytes(b"")
        (root / "index.html").write_bytes(b"<html>public demo</html>\n")
        (root / "data.public.json").write_bytes(b'{"mode":"demo"}\n')
        (root / "status.json").write_bytes(b'{"status":"ok","generatedAt":"2026-10-07T05:40:00Z"}\n')

    def test_manifest_keeps_nojekyll_for_pages_but_does_not_require_http_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            manifest = prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "d" * 40,
            })
            self.assertTrue((site / ".nojekyll").is_file())
            self.assertNotIn(".nojekyll", manifest["files"])

            requested_paths = []

            def reader(url, *, headers):
                path = unquote(urlsplit(url).path).removeprefix("/repo/")
                requested_paths.append(path)
                if path == ".nojekyll":
                    raise AssertionError("Pages control files are not publicly retrievable")
                return (site / path).read_bytes()

            self.assertEqual(
                verify_live_publication(
                    site, "https://owner.github.io/repo",
                    expected_publication_id=manifest["publicationId"], reader=reader,
                    wait=_Clock().wait, monotonic=_Clock().monotonic, max_wait_seconds=0,
                ),
                "VERIFIED",
            )
            self.assertNotIn(".nojekyll", requested_paths)

    def test_manifest_keeps_unique_publication_id_when_source_commit_repeats(self):
        with tempfile.TemporaryDirectory() as directory:
            site_a = Path(directory) / "site-a"
            site_b = Path(directory) / "site-b"
            self._site(site_a)
            self._site(site_b)
            repeated_sha = "c" * 40
            manifest_a = prepare_manifest(site_a, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": repeated_sha,
                "PUBLICATION_WINDOW_DATE": "2026-10-07", "PUBLICATION_WINDOW": "us",
            })
            manifest_b = prepare_manifest(site_b, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "124",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": repeated_sha,
                "PUBLICATION_WINDOW_DATE": "2026-10-07", "PUBLICATION_WINDOW": "us",
            })
            self.assertEqual(manifest_a["sourceCommit"], repeated_sha)
            self.assertEqual(manifest_b["sourceCommit"], repeated_sha)
            self.assertNotEqual(manifest_a["publicationId"], manifest_b["publicationId"])
            self.assertIn("status.json", manifest_a["files"])
            self.assertNotIn("portfolio", json.dumps(manifest_a))

    def test_live_readback_requires_exact_publication_and_all_content_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            expected = prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "b" * 40,
            })
            base_url = "https://owner.github.io/repo"

            def reader(url, *, headers):
                path = unquote(urlsplit(url).path).removeprefix("/repo/")
                return (site / path).read_bytes()

            clock = _Clock()
            self.assertEqual(
                verify_live_publication(site, base_url, expected_publication_id=expected["publicationId"],
                                       reader=reader, wait=clock.wait, monotonic=clock.monotonic, max_wait_seconds=0),
                "VERIFIED",
            )

            original = site / "status.json"
            saved = original.read_bytes()
            original.write_bytes(b'{"status":"old"}\n')
            clock = _Clock()
            with self.assertRaisesRegex(PublicationError, "PUBLICATION_NOT_VISIBLE"):
                verify_live_publication(site, base_url, expected_publication_id=expected["publicationId"],
                                        reader=reader, wait=clock.wait, monotonic=clock.monotonic,
                                        max_wait_seconds=1, poll_seconds=1)
            original.write_bytes(saved)

    def test_newer_verified_publication_wins_without_old_run_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            current = prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "old",
            })
            newer_root = Path(directory) / "newer"
            self._site(newer_root)
            newer = prepare_manifest(newer_root, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "124",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "new",
            })

            def reader(url, *, headers):
                path = unquote(urlsplit(url).path).removeprefix("/repo/")
                return (newer_root / path).read_bytes()

            clock = _Clock()
            self.assertEqual(
                verify_live_publication(site, "https://owner.github.io/repo",
                                       expected_publication_id=current["publicationId"],
                                       reader=reader, wait=clock.wait, monotonic=clock.monotonic, max_wait_seconds=0),
                "SUPERSEDED",
            )

    def test_republication_gets_new_publication_id_but_preserves_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            original = prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "source-commit",
                "PUBLICATION_WINDOW_DATE": "2026-10-08", "PUBLICATION_WINDOW": "tw",
            })
            recovered = prepare_republication(site, source_run_id="123", source_commit="source-commit", env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "200",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "recovery-commit",
            })
            self.assertEqual(recovered["publicationId"], "owner/repo:200:1:1")
            self.assertEqual(recovered["sourceRunId"], "123")
            self.assertEqual(recovered["sourceRunAttempt"], "2")
            self.assertEqual(recovered["sourceCommit"], "source-commit")
            self.assertEqual(recovered["republicationOf"], original["publicationId"])

    def test_republication_rejects_changed_artifact_content(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "source-commit",
            })
            (site / "status.json").write_text('{"status":"changed"}', encoding="utf-8")
            with self.assertRaisesRegex(PublicationError, "content-integrity"):
                prepare_republication(site, source_run_id="123", source_commit="source-commit", env={
                    "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "200",
                    "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "recovery-commit",
                })

    def test_preflight_does_not_overwrite_newer_source_run(self):
        with tempfile.TemporaryDirectory() as directory:
            older_root = Path(directory) / "older"
            self._site(older_root)
            prepare_manifest(older_root, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "old",
            })
            newer_root = Path(directory) / "newer"
            self._site(newer_root)
            prepare_manifest(newer_root, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "124",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "new",
            })

            def reader(url, *, headers):
                path = unquote(urlsplit(url).path).removeprefix("/repo/")
                return (newer_root / path).read_bytes()

            self.assertEqual(
                _publication_preflight(older_root, "https://owner.github.io/repo", reader=reader),
                "SUPERSEDED",
            )

    def test_preflight_allows_repair_of_partial_same_source_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "same-source",
            })

            def reader(url, *, headers):
                path = unquote(urlsplit(url).path).removeprefix("/repo/")
                if path == "status.json":
                    return b'{"status":"partial"}\n'
                return (site / path).read_bytes()

            self.assertEqual(
                _publication_preflight(site, "https://owner.github.io/repo", reader=reader),
                "READY",
            )


if __name__ == "__main__":
    unittest.main()
