import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

from pages_publication import (
    PublicationError,
    pages_build_version,
    prepare_manifest,
    verify_live_publication,
)


class _Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def wait(self, seconds):
        self.value += max(seconds, 0.001)


class PagesPublicationTests(unittest.TestCase):
    def _site(self, root):
        root.mkdir()
        (root / "index.html").write_bytes(b"<html>public demo</html>\n")
        (root / "data.public.json").write_bytes(b'{"mode":"demo"}\n')
        (root / "status.json").write_bytes(b'{"status":"ok","generatedAt":"2026-10-07T05:40:00Z"}\n')

    def test_manifest_records_unique_run_identity_and_public_file_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            manifest = prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "abc123",
                "PUBLICATION_WINDOW_DATE": "2026-10-07", "PUBLICATION_WINDOW": "us",
            })
            self.assertEqual(manifest["publicationId"], "owner/repo:123:2:1")
            self.assertIn("status.json", manifest["files"])
            self.assertNotIn("portfolio", json.dumps(manifest))
            self.assertNotEqual(pages_build_version(manifest["publicationId"], 1), pages_build_version(manifest["publicationId"], 2))

    def test_live_readback_requires_exact_publication_and_all_content_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            self._site(site)
            expected = prepare_manifest(site, env={
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "abc123",
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


if __name__ == "__main__":
    unittest.main()
