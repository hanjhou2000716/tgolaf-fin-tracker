import json
import re
import tempfile
import unittest
from pathlib import Path

from public_site import build_public_payload, build_public_status, write_public_site


class PublicSiteSecurityTests(unittest.TestCase):
    def test_public_contract_is_demo_only(self):
        payload = build_public_payload("2026-08-04T12:00:00+08:00")
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["mode"], "demo")
        self.assertIn("allocation", payload["portfolio"])
        self.assertNotIn("totalAsset", encoded)
        self.assertNotIn("netAsset", encoded)
        self.assertNotIn("assetTree", encoded)
        self.assertNotIn("006208", encoded)
        self.assertNotIn("QQQM", encoded)

    def test_public_status_has_no_portfolio_values(self):
        status = build_public_status("2026-08-04T12:00:00+08:00")
        encoded = json.dumps(status, ensure_ascii=False)
        self.assertEqual(status["mode"], "demo")
        self.assertNotIn("portfolio", encoded)
        self.assertNotIn("totalAsset", encoded)

    def test_written_site_contains_only_safe_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            write_public_site(directory, "2026-08-04T12:00:00+08:00")
            files = {path.name for path in Path(directory).iterdir()}
            self.assertEqual(
                files,
                {
                    "index.html",
                    "data.public.json",
                    "status.json",
                    "private",
                    "PRStK-Remove.png",
                    "SFC.e-removebg-preview.png",
                },
            )
            html = (Path(directory) / "index.html").read_text(encoding="utf-8")
            data = (Path(directory) / "data.public.json").read_text(encoding="utf-8")
            private_html = (Path(directory) / "private" / "index.html").read_text(encoding="utf-8")
            for content in (html, data):
                self.assertNotIn("006208", content)
                self.assertNotIn("QQQM", content)
                self.assertNotIn("assetTree", content)
                self.assertIsNone(re.search(r"NT\$[0-9]", content))
            self.assertIn("telegram.org/js/telegram-web-app.js", html)
            self.assertIn("window.location.replace('./private/')", html)
            self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", private_html)
            self.assertNotIn("server-only-key", private_html)
            self.assertIn("telegram.org/js/telegram-web-app.js", private_html)
            self.assertIn("X-Telegram-Init-Data", private_html)
            self.assertIn("../PRStK-Remove.png", private_html)
            self.assertIn("../SFC.e-removebg-preview.png", private_html)
            self.assertIn('<span class="growth">Growth</span>', private_html)
            self.assertNotIn("Growth · Private", private_html)
            self.assertIn("總資產月線", private_html)
            self.assertIn("總資產季線", private_html)
            self.assertIn("總資產年線", private_html)
            self.assertIn("開啟 Skynet Monitoring", private_html)
            self.assertIn("新增資產資料", private_html)
            self.assertNotIn('target="_blank"', private_html)
            self.assertNotIn("signInWithPassword", private_html)
            self.assertNotIn("SUPABASE_ANON_KEY", private_html)
            self.assertIn("tree-tooltip", private_html)
            self.assertIn("點擊分類查看下一層", private_html)
            self.assertIn("treeBack", private_html)
            self.assertNotIn("allocationMeta", private_html)
            self.assertNotIn("色塊大小依市值比例呈現；點擊分類逐層查看，滑鼠移入可看詳細資訊。", private_html)
            self.assertNotIn('<div class="balance-heading">', private_html)
            self.assertNotIn('class="card-note">淨資產與質押', private_html)
            self.assertNotIn('class="balance-heading"><span>淨資產｜質押</span><small>', private_html)
            self.assertNotIn("以總資產為 100%", private_html)
            self.assertIn("health-card", private_html)
            self.assertNotIn("006208 情境", private_html)
            self.assertIn(".btn.secondary{background:var(--navy)", private_html)
            self.assertIn("equityRatio", private_html)
            self.assertIn("Number.isFinite", private_html)
            self.assertIn("health.sources", private_html)
            self.assertIn("marketQuotes", private_html)
            self.assertIn("unknown", private_html)
            self.assertNotIn('<details class="card health-card" open>', private_html)
            self.assertNotIn("min-height:126px", private_html)
            self.assertNotIn("selectedTreeNode", private_html)
            for removed in ("balanceBar", "balanceTooltip", "balance-bar", "balance-segment", "balance-heading"):
                self.assertNotIn(removed, private_html)
            for hero_id in ("netAsset", "equityRatio", "dailyChange", "sync", "totalAsset", "totalDebt", "debtRatio"):
                self.assertIn(f'id="{hero_id}"', private_html)
            self.assertIn("hero-header", private_html)
            self.assertIn("sync-meta", private_html)

    def test_private_hero_uses_vertical_kpi_stack_and_single_divider(self):
        with tempfile.TemporaryDirectory() as directory:
            write_public_site(directory, "2026-08-12T10:37:00+08:00")
            private_html = (Path(directory) / "private" / "index.html").read_text(encoding="utf-8")
            self.assertIn('class="hero-kpi-stack"', private_html)
            self.assertIn('class="net-value-group"', private_html)
            self.assertIn('class="hero-divider"', private_html)
            self.assertIn('id="dailyChange" class="pill"', private_html)
            self.assertIn('id="equityRatio" class="equity-ratio"', private_html)
            self.assertIn(".hero-kpi-stack{position:relative", private_html)
            self.assertIn("flex-direction:column", private_html)
            self.assertIn(".hero-kpi-stack>.pill{align-self:flex-start", private_html)
            self.assertNotIn("hero-line", private_html)
            self.assertNotIn("hero-kpi-row", private_html)
            self.assertNotIn("status-row", private_html)
            self.assertNotIn("股權比", private_html)
            self.assertNotIn("淨值率", private_html)

    def test_private_hero_has_invalid_ratio_and_sync_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            write_public_site(directory, "2026-08-12T10:37:00+08:00")
            private_html = (Path(directory) / "private" / "index.html").read_text(encoding="utf-8")
            self.assertIn("Number.isFinite(n)&&Number.isFinite(t)&&t>0", private_html)
            self.assertIn(": '(—)'", private_html)
            self.assertIn("syncText", private_html)
            self.assertIn("同步 —", private_html)
            self.assertNotIn("資料同步 ·", private_html)
            self.assertIn("Asia/Taipei", private_html)
            self.assertIn("daily.textContent='今日 —'", private_html)

    def test_private_hero_is_single_source_and_content_length_safe(self):
        source = Path("public_site.py").read_text(encoding="utf-8")
        for removed in ("PRIVATE_HERO_MARKUP", "PRIVATE_HERO_STYLE", "_prepare_private_dashboard_html", "import re"):
            self.assertNotIn(removed, source)
        with tempfile.TemporaryDirectory() as directory:
            write_public_site(directory, "2026-08-12T10:37:00+08:00")
            private_html = (Path(directory) / "private" / "index.html").read_text(encoding="utf-8")
            self.assertIn('class="hero-kpi-stack"', private_html)
            self.assertIn('id="dailyChange" class="pill"', private_html)
            self.assertIn('class="hero-divider"', private_html)
            self.assertNotIn("width:100%", private_html[private_html.index('id="dailyChange"') - 200:private_html.index('id="dailyChange"') + 200])
            self.assertNotIn("z-index:30", private_html[private_html.index('class="hero"'):private_html.index('class="hero"') + 2500])


if __name__ == "__main__":
    unittest.main()
