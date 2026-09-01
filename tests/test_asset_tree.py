import unittest

from asset_tree import asset_tree_metadata_summary, build_asset_tree


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
        self.assertEqual(etf_leaf["assetClass"], "stock")
        self.assertEqual(etf_leaf["shares"], 48000)
        self.assertEqual(etf_leaf["pledgedShares"], 17500)
        tsmc = next(group for group in tw["children"] if group["label"] == "台積電")
        self.assertEqual(tsmc["children"][0]["shares"], 2000)
        self.assertNotIn("pledgedShares", tsmc["children"][0])
        us = next(child for child in tree["children"] if child["label"] == "現貨美股")
        us_leaf = us["children"][0]["children"][0]
        self.assertEqual(us_leaf["shares"], 12.345)
        self.assertNotIn("shares", next(child for child in tree["children"] if child["label"] == "現金與基金")["children"][0])
        self.assertNotIn("assetClass", next(child for child in tree["children"] if child["label"] == "現金與基金")["children"][0])

    def test_invalid_or_non_positive_metadata_is_omitted(self):
        tree = build_asset_tree(
            {"006208": 100}, {}, 0, {},
            tw_shares={"006208": "not-a-number"},
            pledged_shares={"006208": -1},
        )
        leaf = tree["children"][0]["children"][0]["children"][0]
        self.assertEqual(leaf["assetClass"], "stock")
        self.assertNotIn("shares", leaf)
        self.assertNotIn("pledgedShares", leaf)

    def test_metadata_summary_reports_missing_share_metadata_without_financial_values(self):
        tree = build_asset_tree(
            {"006208": 500, "2330": 200}, {}, 0, {},
            tw_shares={"006208": 100}, pledged_shares={"006208": 50},
        )
        summary = asset_tree_metadata_summary(tree)
        self.assertEqual(summary["stockLeafCount"], 2)
        self.assertEqual(summary["stockLeavesWithShares"], 1)
        self.assertEqual(summary["stockLeavesMissingShares"], 1)
        self.assertEqual(summary["stockLeavesWithCollateral"], 1)
        self.assertFalse(summary["complete"])
        self.assertNotIn("value", summary)

    def test_splits_twd_and_usd_cash_without_changing_cash_subtotal(self):
        tree = build_asset_tree(
            {}, {}, 150000, {},
            cash_twd_value=78000,
            cash_usd_twd_value=72000,
            cash_usd_native=2250,
        )
        cash_group = tree["children"][0]
        self.assertEqual(cash_group["label"], "現金與基金")
        self.assertEqual(cash_group["value"], 150000)
        self.assertEqual([child["label"] for child in cash_group["children"]], ["台幣現金", "美金現金"])
        twd, usd = cash_group["children"]
        self.assertEqual(twd["value"], 78000)
        self.assertEqual(twd["currency"], "TWD")
        self.assertEqual(usd["value"], 72000)
        self.assertEqual(usd["currency"], "USD")
        self.assertEqual(usd["nativeAmount"], 2250)
        summary = asset_tree_metadata_summary(tree)
        self.assertEqual(summary["cashLeafCount"], 2)
        self.assertEqual(summary["cashTwdLeafCount"], 1)
        self.assertEqual(summary["cashUsdLeafCount"], 1)
        self.assertEqual(summary["cashUsdLeavesWithNativeAmount"], 1)
        self.assertEqual(summary["cashLabels"], ["台幣現金", "美金現金"])

    def test_cash_breakdown_omits_zero_balances_and_preserves_funds(self):
        twd_only = build_asset_tree({}, {}, 150000, {"FUND": 27000}, cash_twd_value=150000, cash_usd_twd_value=0, cash_usd_native=0)
        cash_group = next(child for child in twd_only["children"] if child["label"] == "現金與基金")
        self.assertEqual([child["label"] for child in cash_group["children"]], ["台幣現金", "FUND"])
        self.assertNotIn("nativeAmount", cash_group["children"][0])

        usd_only = build_asset_tree({}, {}, 32000, {}, cash_twd_value=0, cash_usd_twd_value=32000, cash_usd_native=1000)
        usd_leaf = usd_only["children"][0]["children"][0]
        self.assertEqual(usd_leaf["label"], "美金現金")
        self.assertEqual(usd_leaf["value"], 32000)
        self.assertEqual(usd_leaf["nativeAmount"], 1000)

        fund_only = build_asset_tree({}, {}, 0, {"FUND": 27000}, cash_twd_value=0, cash_usd_twd_value=0, cash_usd_native=0)
        self.assertEqual(fund_only["children"][0]["children"][0]["label"], "FUND")

    def test_cash_split_preserves_root_and_non_cash_values(self):
        before = build_asset_tree({"006208": 500000}, {"QQQM": 300000}, 150000, {"FUND": 27000})
        after = build_asset_tree(
            {"006208": 500000}, {"QQQM": 300000}, 150000, {"FUND": 27000},
            cash_twd_value=78000, cash_usd_twd_value=72000, cash_usd_native=2250,
        )
        self.assertEqual(after["value"], before["value"])
        self.assertEqual(
            [(child["label"], child["value"]) for child in after["children"] if child["label"] != "現金與基金"],
            [(child["label"], child["value"]) for child in before["children"] if child["label"] != "現金與基金"],
        )


if __name__ == "__main__":
    unittest.main()
