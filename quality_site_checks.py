"""Build and audit the anonymous static Demo without touching private data."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tempfile

from public_site import write_public_site


PRIVATE_MARKERS = re.compile(r"006208|QQQM|2330|TSM|NT\$[0-9]", re.IGNORECASE)
SERVICE_KEY_MARKERS = re.compile(r"SUPABASE_SERVICE_ROLE_KEY|service_role_key", re.IGNORECASE)
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc\s*=)[^>]*>([\s\S]*?)</script\s*>", re.IGNORECASE)


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="growth-public-demo-") as temporary:
        site = Path(temporary)
        write_public_site(str(site), "2026-10-08T00:00:00Z")
        public_files = [site / "index.html", site / "data.public.json", site / "status.json"]
        if any(not path.is_file() or not path.stat().st_size for path in public_files):
            raise RuntimeError("public Demo build is missing a required output")
        for path in public_files:
            content = path.read_text(encoding="utf-8")
            if PRIVATE_MARKERS.search(content) or SERVICE_KEY_MARKERS.search(content):
                raise RuntimeError(f"public Demo sanitizer rejected {path.name}")
        json.loads((site / "data.public.json").read_text(encoding="utf-8"))
        json.loads((site / "status.json").read_text(encoding="utf-8"))

        scripts = []
        for html_path in (site / "index.html", site / "private" / "index.html"):
            html = html_path.read_text(encoding="utf-8")
            scripts.extend(INLINE_SCRIPT.findall(html))
        if not scripts:
            raise RuntimeError("static Demo contains no inline JavaScript to validate")
        for index, script in enumerate(scripts):
            script_path = site / f"inline-{index}.js"
            script_path.write_text(script, encoding="utf-8")
            subprocess.run(["node", "--check", str(script_path)], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    run()
    print("Static Demo build, sanitizer, and JavaScript syntax checks passed")
