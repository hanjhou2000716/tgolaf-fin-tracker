import tempfile
import unittest
from pathlib import Path

from public_site import (
    BUYHOLD_LAMP_ASSET,
    BUYHOLD_LAMP_NAMES,
    BUYHOLD_LAMP_STATE_CLASSES,
    buyhold_lamp_state,
    canonical_buyhold_lamp_code,
    render_buyhold_lamp,
    write_public_site,
)


class BuyHoldLampContractTests(unittest.TestCase):
    def test_all_codes_and_unknown_values_have_one_safe_state(self):
        for code, css_class in BUYHOLD_LAMP_STATE_CLASSES.items():
            with self.subTest(code=code):
                self.assertEqual(buyhold_lamp_state(code), (css_class, BUYHOLD_LAMP_NAMES[code]))

        self.assertEqual(buyhold_lamp_state(" blue "), ("is-blue", "藍燈"))
        self.assertEqual(canonical_buyhold_lamp_code(" green "), "GREEN")
        self.assertEqual(canonical_buyhold_lamp_code("unknown"), "UNAVAILABLE")
        self.assertEqual(buyhold_lamp_state("not-a-light"), ("is-unavailable", "資料暫不可用"))
        self.assertEqual(buyhold_lamp_state(None), ("is-unavailable", "資料暫不可用"))

    def test_markup_is_a_single_accessible_lamp_without_visible_emoji(self):
        markup = render_buyhold_lamp("RED", element_id="testLamp", asset_path="assets/lamp.png")
        self.assertIn('id="testLamp"', markup)
        self.assertIn('class="buyhold-lamp is-red"', markup)
        self.assertIn('role="img"', markup)
        self.assertIn('aria-label="當前燈號：紅燈"', markup)
        self.assertIn('title="紅燈"', markup)
        self.assertEqual(markup.count("buyhold-lamp__lens"), 1)
        self.assertEqual(markup.count("buyhold-lamp__shell"), 1)
        self.assertIn('alt=""', markup)
        self.assertNotIn("🔴", markup)

    def test_written_pages_share_the_hashed_rgba_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            write_public_site(directory, "2026-09-10T08:00:00+08:00")
            root = Path(directory)
            asset = root / BUYHOLD_LAMP_ASSET
            self.assertTrue(asset.is_file())
            self.assertGreaterEqual(asset.stat().st_size, 512)
            public_html = (root / "index.html").read_text(encoding="utf-8")
            private_html = (root / "private" / "index.html").read_text(encoding="utf-8")
            for html, path in ((public_html, BUYHOLD_LAMP_ASSET), (private_html, f"../{BUYHOLD_LAMP_ASSET}")):
                self.assertIn('class="buyhold-lamp is-unavailable"', html)
                self.assertIn('class="buyhold-lamp__lens"', html)
                self.assertIn(path, html)
            lamp_block = private_html[private_html.index('<div id="buyHoldLight"'):private_html.index('</div>', private_html.index('<div id="buyHoldLight"')) + 6]
            self.assertNotIn("⚪", lamp_block)
            self.assertNotIn("🔵", lamp_block)
            self.assertNotIn("🟢", lamp_block)
            self.assertNotIn("🟡", lamp_block)
            self.assertNotIn("🟠", lamp_block)
            self.assertNotIn("🔴", lamp_block)

            self.assertIn("setBuyHoldLamp", private_html)
            self.assertIn("element.classList.remove(...buyHoldLampClassNames)", private_html)
            self.assertIn("const bhCode=setBuyHoldLamp(lightElement,bhCodeRaw)", private_html)
            self.assertNotIn("lightElement.textContent=bhLight.emoji", private_html)

    def test_shell_is_rgba_large_and_corners_are_transparent(self):
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover - Pillow is provided by matplotlib in CI
            self.skipTest("Pillow is unavailable")
        asset = Path(__file__).resolve().parents[1] / BUYHOLD_LAMP_ASSET
        with Image.open(asset) as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertGreaterEqual(image.width, 512)
            self.assertGreaterEqual(image.height, 512)
            alpha = image.getchannel("A")
            self.assertEqual(alpha.getpixel((0, 0)), 0)
            self.assertEqual(alpha.getpixel((image.width - 1, image.height - 1)), 0)
            self.assertLessEqual(alpha.getpixel((image.width // 2, image.height // 2)), 32)


if __name__ == "__main__":
    unittest.main()
