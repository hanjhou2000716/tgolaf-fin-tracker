from datetime import datetime, timezone
import unittest

from runtime_extensions import build_runtime_extensions


class RuntimeExtensionTests(unittest.TestCase):
    def test_snapshot_drives_all_private_sections(self):
        result = build_runtime_extensions(
            net_values=[1000, 1010, 990, 1040],
            net_asset=1040,
            total_asset=1240,
            total_debt=200,
            pledged_value=400,
            data_as_of="2026-08-04T12:00:00+00:00",
            sources={"quotes": {"quality": "fresh", "source": "test"}},
            now=datetime(2026, 8, 4, 12, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(result["performanceReport"])
        self.assertIsNotNone(result["regime"])
        self.assertIsNotNone(result["goalForecast"])
        self.assertEqual(set(result["marginCallProbability"]), {"1", "5", "20", "60"})
        self.assertIn("dataHealth", result)
        self.assertFalse(result["advisor"]["isTradeInstruction"])

    def test_short_history_is_explicitly_unavailable(self):
        result = build_runtime_extensions(
            net_values=[1000], net_asset=1000, total_asset=1000, total_debt=0,
            pledged_value=0, data_as_of="2026-08-04T12:00:00+00:00", sources={},
        )
        self.assertIsNone(result["performanceReport"])
        self.assertIsNone(result["regime"])


if __name__ == "__main__":
    unittest.main()
