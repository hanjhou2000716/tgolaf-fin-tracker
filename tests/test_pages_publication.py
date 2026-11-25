import json
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from urllib.error import HTTPError

from pages_publication import (
    GitHubAPIError,
    PublicationError,
    _publication_preflight,
    _validate_pages_artifact,
    pages_build_version,
    prepare_manifest,
    prepare_republication,
    verify_live_publication,
    _request_json,
    _oidc_token,
    _publish_pages,
    publish_and_verify,
    main as publication_main,
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

    def test_pages_artifact_lookup_uses_exact_uploader_id_and_run(self):
        payload = {
            "id": 987654,
            "name": "github-pages",
            "expired": False,
            "size_in_bytes": 1024,
            "workflow_run": {"id": 123},
        }
        with patch("pages_publication._request_json", return_value=payload) as request:
            result = _validate_pages_artifact(
                "owner/repo", "123", "987654", "token", max_wait_seconds=0,
            )
        self.assertEqual(result["id"], 987654)
        self.assertEqual(request.call_args.args[0], "GET")
        self.assertEqual(request.call_args.args[1], "https://api.github.com/repos/owner/repo/actions/artifacts/987654")

    def test_pages_artifact_lookup_retries_read_after_write_404_without_listing(self):
        payload = {
            "id": 987654, "name": "github-pages", "expired": False,
            "size_in_bytes": 1, "workflow_run": {"id": 123},
        }
        clock = _Clock()
        with patch("pages_publication._request_json", side_effect=[
            GitHubAPIError("not visible", method="GET", status_code=404),
            payload,
        ]) as request:
            result = _validate_pages_artifact(
                "owner/repo", "123", 987654, "token",
                wait=clock.wait, monotonic=clock.monotonic, max_wait_seconds=60,
            )
        self.assertEqual(result, payload)
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(call.args[1].endswith("/actions/artifacts/987654") for call in request.call_args_list))
        self.assertGreater(clock.value, 0)

    def test_pages_artifact_lookup_rejects_wrong_id_run_name_expiry_and_empty_payload(self):
        cases = [
            ({"id": 1, "name": "github-pages", "expired": False, "workflow_run": {"id": 123}}, "different Pages artifact ID"),
            ({"id": 987654, "name": "other", "expired": False, "workflow_run": {"id": 123}}, "unexpected name"),
            ({"id": 987654, "name": "github-pages", "expired": True, "workflow_run": {"id": 123}}, "expired"),
            ({"id": 987654, "name": "github-pages", "expired": False, "workflow_run": {"id": 999}}, "different workflow run"),
            ({"id": 987654, "name": "github-pages", "expired": False, "size_in_bytes": 0, "workflow_run": {"id": 123}}, "empty"),
        ]
        for payload, reason in cases:
            with self.subTest(reason=reason), patch("pages_publication._request_json", return_value=payload):
                with self.assertRaisesRegex(PublicationError, reason):
                    _validate_pages_artifact("owner/repo", "123", 987654, "token", max_wait_seconds=0)
        for invalid in ("", "not-an-id", "0", "-4"):
            with self.subTest(invalid=invalid), patch("pages_publication._request_json") as request:
                with self.assertRaisesRegex(PublicationError, "valid artifact ID"):
                    _validate_pages_artifact("owner/repo", "123", invalid, "token")
                request.assert_not_called()

    def test_prepare_republication_binds_source_attempt_and_origin_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            original = prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40,
                "PUBLICATION_WINDOW_DATE": "2026-10-16", "PUBLICATION_WINDOW": "tw",
            })
            with self.assertRaisesRegex(PublicationError, "attempt"):
                prepare_republication(
                    site, source_run_id="123", source_run_attempt="1", source_commit="a" * 40,
                    env={"GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "200",
                         "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "b" * 40},
                )
            recovered = prepare_republication(
                site, source_run_id="123", source_run_attempt="2", source_commit="a" * 40,
                env={"GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "200",
                     "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "b" * 40},
            )
            self.assertEqual(recovered["republicationOf"], original["publicationId"])
            self.assertEqual(recovered["originContentHash"], original["contentHash"])
            self.assertEqual(recovered["sourceRunAttempt"], "2")

    def test_publisher_requires_uploader_id_and_matching_active_attempt_before_any_api_call(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40,
            })
            base_env = {
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40,
                "PAGES_BASE_URL": "https://owner.github.io/repo", "GH_TOKEN": "token",
            }
            with patch("pages_publication._request_json") as request:
                with self.assertRaisesRegex(PublicationError, "artifact ID is missing"):
                    __import__("pages_publication").publish_and_verify(site, env=base_env)
                request.assert_not_called()

            with patch("pages_publication._publication_preflight", return_value="READY"), \
                 patch("pages_publication._validate_pages_artifact") as validate:
                wrong_attempt = {**base_env, "GITHUB_RUN_ATTEMPT": "3", "PAGES_ARTIFACT_ID": "987654"}
                with self.assertRaisesRegex(PublicationError, "active workflow attempt"):
                    __import__("pages_publication").publish_and_verify(site, env=wrong_attempt)
                validate.assert_not_called()

    def test_publisher_deploys_the_exact_artifact_id_returned_by_this_run(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40,
            })
            env = {
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40,
                "PAGES_BASE_URL": "https://owner.github.io/repo", "GH_TOKEN": "token",
                "PAGES_ARTIFACT_ID": "987654",
            }
            verification = {
                "status": "VERIFIED", "publicationId": "owner/repo:123:2:1",
                "observedPublicationId": "owner/repo:123:2:1",
            }
            with patch("pages_publication._publication_preflight", return_value="READY"), \
                 patch("pages_publication._validate_pages_artifact", return_value={"id": 987654}) as validate, \
                 patch("pages_publication._oidc_token", return_value="oidc"), \
                 patch("pages_publication._publish_pages", return_value=("https://api.github.test/status", "https://owner.github.io/repo")) as publish, \
                 patch("pages_publication._await_deployment"), \
                 patch("pages_publication.verify_live_publication", return_value=verification):
                result = publish_and_verify(site, env=env)
            validate.assert_called_once_with(
                "owner/repo", "123", "987654", "token", wait=unittest.mock.ANY,
                monotonic=unittest.mock.ANY, max_wait_seconds=60,
            )
            publish.assert_called_once_with("owner/repo", 987654, "a" * 40, 1, "oidc", "token")
            self.assertEqual(result["artifactId"], 987654)
            self.assertEqual(result["status"], "VERIFIED")

    def test_live_readback_details_capture_observed_manifest_identity_and_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            manifest = prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40,
                "PUBLICATION_WINDOW_DATE": "2026-10-16", "PUBLICATION_WINDOW": "tw",
            })

            def reader(url, *, headers):
                path = unquote(urlsplit(url).path).removeprefix("/repo/")
                return (site / path).read_bytes()

            details = __import__("pages_publication").verify_live_publication(
                site, "https://owner.github.io/repo", expected_publication_id=manifest["publicationId"],
                reader=reader, wait=lambda _seconds: None, monotonic=lambda: 0,
                max_wait_seconds=0, return_details=True,
            )
            self.assertEqual(details["status"], "VERIFIED")
            self.assertEqual(details["observedPublicationId"], manifest["publicationId"])
            self.assertEqual(details["observedContentHash"], manifest["contentHash"])
            self.assertEqual(details["observedFilesHash"], details["filesHash"])
            self.assertEqual(details["observedSourceRunAttempt"], "2")

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

    def test_live_verification_artifact_binds_scope_to_exact_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site = root / "site"
            self._site(site)
            manifest = prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40,
                "PUBLICATION_WINDOW_DATE": "2026-10-16", "PUBLICATION_WINDOW": "tw",
            })
            summary = root / "publication-verification.json"
            with patch.dict(os.environ, {"PUBLICATION_SUMMARY_PATH": str(summary)}):
                with patch("pages_publication.verify_live_publication", return_value={
                    "status": "VERIFIED", "observedPublicationId": manifest["publicationId"],
                    "observedContentHash": manifest["contentHash"], "observedStatus": "VERIFIED",
                }):
                    self.assertEqual(publication_main([
                        "verify-live", str(site), "--base-url", "https://owner.github.io/repo",
                    ]), 0)
            saved = json.loads(summary.read_text(encoding="utf-8"))
            self.assertEqual(saved["publicationId"], manifest["publicationId"])
            self.assertEqual(saved["runId"], "123")
            self.assertEqual(saved["sourceCommit"], "a" * 40)
            self.assertEqual(saved["windowDate"], "2026-10-16")
            self.assertEqual(saved["window"], "tw")
            self.assertEqual(saved["contentHash"], manifest["contentHash"])
            self.assertEqual(saved["observedPublicationId"], manifest["publicationId"])
            self.assertEqual(saved["schemaVersion"], 2)

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
