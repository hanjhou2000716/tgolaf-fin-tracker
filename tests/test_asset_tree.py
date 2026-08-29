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

    def test_stock_leaves_carry_share_metadata_without_changing_values(self):
        tree = build_asset_tree(
            {"006208": 500000, "2330": 200000},
            {"QQM": 12345},
            50,
            {"FUND": 25},
            tw_shares={"006208": 48000, "2330": 2000},
            us_shares={"QQM": 12.345},
            pledged_shares={"006208": 17500, "2330": 0},
        )
        self.assertEqual(tree["value"], 712420)
        tw = next(child for child in tree["children"] if child["label"] == "現貨台股")
        etf = next(group for group in tw["children"] if group["label"] == "台股市值型")
        etf_leaf = etf["children"][0]
        self.assertEqual(etf_leaf["shares"], 48000)
        self.assertEqual(etf_leaf["pledgedShares"], 17500)
        tsmc = next(group for group in tw["children"] if group["label"] == "台積電")
        self.assertEqual(tsmc["children"][0]["shares"], 2000)
        self.assertNotIn("pledgedShares", tsmc["children"][0])
        us = next(child for child in tree["children"] if child["label"] == "現貨美股")
        us_leaf = us["children"][0]["children"][0]
        self.assertEqual(us_leaf["shares"], 12.345)
        self.assertNotIn("shares", next(child for child in tree["children"] if child["label"] == "現金與基金")["children"][0])

    def test_invalid_or_non_positive_metadata_is_omitted(self):
        tree = build_asset_tree(
            {"006208": 100}, {}, 0, {},
            tw_shares={"006208": "not-a-number"},
            pledged_shares={"006208": -1},
        )
        leaf = tree["children"][0]["children"][0]["children"][0]
        self.assertNotIn("shares", leaf)
        self.assertNotIn("pledgedShares", leaf)


if __name__ == "__main__":
    unittest.main()
