import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TelegramEntrypointTests(unittest.TestCase):
    def test_growth_button_opens_private_webapp_route(self):
        source = (ROOT / "dashboard_pipeline.py").read_text(encoding="utf-8")
        self.assertIn('WEB_APP_URL = "https://hanjhou2000716.github.io/tgolaf-fin-tracker/private/"', source)
        self.assertIn('"🌱 Growth儀表板"', source)


    def test_settlement_push_keeps_message_compact(self):
        source = (ROOT / "dashboard_pipeline.py").read_text(encoding="utf-8")
        self.assertNotIn("市場損益 {performance", source)
        self.assertNotIn("外部現金流 {performance", source)
        self.assertNotIn("融資現金流 {performance", source)

    def test_settlement_message_uses_light_prefix_without_buy_hold_third_line(self):
        source = (ROOT / "dashboard_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("build_settlement_telegram_message", source)
        self.assertIn("buy_hold_telegram_emoji", source)
        self.assertNotIn("buy_hold_telegram_line", source)
        self.assertNotIn('tg_text += "\\n" + buy_hold_telegram_line(buy_hold_policy)', source)
        self.assertNotIn("Telegram Buy&Hold line sent", source)
        self.assertIn("Telegram settlement light sent", source)
        self.assertNotIn("🚀 厲害的阿洲", source)
        self.assertNotIn("💸 可憐的阿洲", source)
        self.assertIn("completed-session-close", (ROOT / "buy_hold_policy.py").read_text(encoding="utf-8"))
        self.assertIn("get_taiex_history", source)
        self.assertIn("buyhold-market-data-summary.json", source)
        self.assertNotIn('yf.Ticker("^TWII").history(period="2y"', source)
        self.assertNotIn('yf.Ticker("^TWII").history(period="200d"', source)

    def test_buy_hold_card_sits_between_leverage_and_exposure(self):
        source = (ROOT / "dashboard_pipeline.py").read_text(encoding="utf-8")
        dashboard = source.split('html_content = f"""', 1)[1]
        self.assertLess(dashboard.index("槓桿 <span"), dashboard.index("{buy_hold_section_html}"))
        self.assertLess(dashboard.index("{buy_hold_section_html}"), dashboard.index("曝險 <span"))
        self.assertIn('"buyHold": buy_hold_policy', dashboard)
        self.assertIn("buyhold-metrics-panel", source)
        self.assertIn("buyhold-metric--drawdown", source)
        self.assertIn("buyhold-metric--next", source)
        self.assertIn("TX目前回撤", source)
        self.assertNotIn("buyhold-metrics-divider", source)
        css = (ROOT / "public_site.py").read_text(encoding="utf-8")
        self.assertIn("buyhold-metrics-panel.is-green", css)
        self.assertIn("buyhold-metrics-panel.is-yellow", css)
        self.assertIn("buyhold-metrics-panel.is-orange", css)
        self.assertIn("buyhold-metrics-panel.is-red", css)
        self.assertIn("buyhold-metrics-panel.is-unavailable", css)
        self.assertIn(".buyhold-metric--next", css)
        self.assertIn("border-top:1px solid #d5ddd5", css)
        self.assertNotIn("buyhold-divider", dashboard)
        self.assertNotIn("buyhold-metrics-rail", dashboard)
        self.assertNotIn("buyhold-metric--drawdown.is-up", dashboard)
        self.assertNotIn("buyhold-metric--drawdown.is-down", dashboard)
        self.assertNotIn("buyhold-metric--drawdown.is-flat", dashboard)
        self.assertNotIn("buyhold-info-grid", dashboard)
        self.assertNotIn("buyhold-gate", dashboard)
        self.assertNotIn("距—", dashboard)


if __name__ == "__main__":
    unittest.main()
