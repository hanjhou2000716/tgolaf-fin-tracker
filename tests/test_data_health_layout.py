import tempfile
import unittest
from pathlib import Path

from public_site import write_public_site


class DataHealthLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as directory:
            write_public_site(directory, "2026-08-12T10:37:00+08:00")
            cls.html = (Path(directory) / "private" / "index.html").read_text(
                encoding="utf-8"
            )

    def test_health_summary_has_compact_native_details_contract(self):
        self.assertIn('<summary class="card-title health-summary">', self.html)
        self.assertIn(
            ".health-card > summary.health-summary{display:flex;align-items:center;"
            "justify-content:space-between;gap:12px;min-height:46px;margin:0;"
            "padding:0 18px;cursor:pointer;list-style:none}",
            self.html,
        )
        self.assertIn(
            ".health-card > summary.health-summary:focus-visible{outline:2px solid var(--orange);",
            self.html,
        )
        self.assertIn(
            ".health-card[open] > summary.health-summary:after{content:'－'}",
            self.html,
        )
        self.assertIn(
            "healthSummary=healthCard?.querySelector('.health-summary')",
            self.html,
        )
        self.assertIn("event.key===' '||event.key==='Spacebar'", self.html)

    def test_mobile_health_card_overrides_generic_card_padding(self):
        self.assertIn(
            "@media(max-width:560px){body{padding:14px 10px 34px}.hero,.card{padding:15px}.health-card{padding:0}",
            self.html,
        )
        self.assertIn(
            ".health-card > summary.health-summary{padding:0 15px;min-height:46px}",
            self.html,
        )
        self.assertIn(
            ".health-card .health{margin:14px 15px 9px}.health-card #advisor{margin:0 15px 14px}",
            self.html,
        )
        self.assertIn(".card{margin-top:12px;padding:18px", self.html)

    def test_closed_default_and_open_body_have_no_fixed_height_hack(self):
        self.assertIn('<details class="card health-card">', self.html)
        self.assertNotIn('<details class="card health-card" open>', self.html)
        self.assertNotIn("details{height:", self.html)
        self.assertNotIn("height:48px", self.html)
        self.assertIn(".health-card .health{margin:14px 18px 9px}", self.html)
        self.assertIn(".health-card #advisor{margin:0 18px 14px}", self.html)

    def test_recent_updates_are_inside_health_card_only(self):
        self.assertIn('class="health-subsection"', self.html)
        self.assertIn('id="transactionSummary"', self.html)
        self.assertIn('id="transactionIngestion"', self.html)
        self.assertNotIn(
            '<section class="section"><div class="section-heading"><h2>最近資產更新</h2>',
            self.html,
        )
        self.assertLess(
            self.html.index('id="transactionIngestion"'),
            self.html.index('</details></section>'),
        )

    def test_recent_update_summary_and_mobile_rows_are_present(self):
        self.assertIn("dataset.ingestionCount", self.html)
        self.assertIn("目前沒有交易狀態紀錄", self.html)
        self.assertIn(
            ".ingestion-status.applied_with_compatibility,.ingestion-status.pending{color:var(--orange)}",
            self.html,
        )
        self.assertIn(".ingestion-status.rejected{color:var(--brick)}", self.html)
        self.assertIn(".health-subsection{margin:0 18px 14px", self.html)
        self.assertIn(
            ".health-card .ingestion-row{grid-template-columns:1fr;gap:3px;padding:8px 9px}",
            self.html,
        )

    def test_existing_action_buttons_remain_blue_and_miniapp_safe(self):
        self.assertIn(".btn.secondary{background:var(--navy);color:#fff;border-color:var(--navy)}", self.html)
        self.assertNotIn('target="_blank"', self.html)

    def test_private_miniapp_has_recent_transaction_status_contract(self):
        self.assertIn('id="transactionIngestion"', self.html)
        self.assertIn('renderIngestion', self.html)
        self.assertIn('transactionIngestion', self.html)


if __name__ == "__main__":
    unittest.main()
