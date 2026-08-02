import unittest

from asset_tree import build_asset_tree


class AssetTreeTests(unittest.TestCase):
    def test_builds_hierarchy_without_liabilities(self):
        tree = build_asset_tree(
            {"006208": 500, "2330": 200, "00685L": 100},
            {"QQQM": 300, "TSM": 100},
            50,
            {"FUND": 25},
        )
        self.assertEqual(tree["value"], 1275)
        self.assertEqual(sum(child["value"] for child in tree["children"]), tree["value"])
        self.assertNotIn("質押借款", [child["label"] for child in tree["children"]])
        tw = next(child for child in tree["children"] if child["label"] == "現貨台股")
        self.assertEqual(tw["value"], 800)
        self.assertEqual(next(group for group in tw["children"] if group["label"] == "台股市值型")["value"], 500)

    def test_ignores_zero_and_history_rows(self):
        tree = build_asset_tree(
            {"006208": 0, "2330": 100},
            {},
            0,
            {"History": 999, "FUND": 0},
        )
        self.assertEqual(tree["value"], 100)
        self.assertEqual(len(tree["children"]), 1)
        self.assertEqual(tree["children"][0]["label"], "現貨台股")


if __name__ == "__main__":
    unittest.main()
