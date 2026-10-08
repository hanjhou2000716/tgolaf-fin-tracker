"""Build and audit the anonymous static Demo without touching private data."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import tempfile

from public_site import write_public_site


PRIVATE_MARKERS = ("006208", "QQQM", "2330", "TSM", "NT$")
SERVICE_KEY_MARKERS = ("SUPABASE_SERVICE_ROLE_KEY", "service_role_key")


class InlineScriptCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self._capturing = False
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "script":
            return
        self._capturing = not any(name.lower() == "src" for name, _ in attrs)
        self._parts = []

    def handle_data(self, data):
        if self._capturing:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._capturing:
            self.scripts.append("".join(self._parts))
            self._capturing = False
            self._parts = []


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="growth-public-demo-") as temporary:
        site = Path(temporary)
        write_public_site(str(site), "2026-10-08T00:00:00Z")
        public_files = [site / "index.html", site / "data.public.json", site / "status.json"]
        if any(not path.is_file() or not path.stat().st_size for path in public_files):
            raise RuntimeError("public Demo build is missing a required output")
        for path in public_files:
            content = path.read_text(encoding="utf-8").upper()
            if any(marker in content for marker in PRIVATE_MARKERS) or any(
                marker.upper() in content for marker in SERVICE_KEY_MARKERS
            ):
                raise RuntimeError(f"public Demo sanitizer rejected {path.name}")
        json.loads((site / "data.public.json").read_text(encoding="utf-8"))
        json.loads((site / "status.json").read_text(encoding="utf-8"))

        scripts = []
        for html_path in (site / "index.html", site / "private" / "index.html"):
            parser = InlineScriptCollector()
            parser.feed(html_path.read_text(encoding="utf-8"))
            scripts.extend(parser.scripts)
        if not scripts:
            raise RuntimeError("static Demo contains no inline JavaScript to validate")
        for index, script in enumerate(scripts):
            script_path = site / f"inline-{index}.js"
            script_path.write_text(script, encoding="utf-8")
            subprocess.run(["node", "--check", str(script_path)], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    run()
    print("Static Demo build, sanitizer, and JavaScript syntax checks passed")
