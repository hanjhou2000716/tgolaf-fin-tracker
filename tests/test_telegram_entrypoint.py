import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TelegramEntrypointTests(unittest.TestCase):
    def test_growth_button_opens_private_webapp_route(self):
        source = (ROOT / "dashboard_pipeline.py").read_text(encoding="utf-8")
        self.assertIn('WEB_APP_URL = "https://hanjhou2000716.github.io/tgolaf-fin-tracker/private/"', source)
        self.assertIn('"🌱 開啟Growth儀表板"', source)


if __name__ == "__main__":
    unittest.main()
