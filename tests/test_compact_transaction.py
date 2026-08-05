import unittest
from decimal import Decimal

from compact_transaction import parse_compact_transaction
from transaction_schema import Action, parse_transaction_rows


class CompactTransactionTests(unittest.TestCase):
    def test_buy_lots_is_normalized_to_shares(self):
        tx = parse_compact_transaction("買入 006208 2 張，價格 55.30")
        self.assertEqual(tx.action, Action.BUY)
        self.assertEqual(tx.asset_type, "台股")
        self.assertEqual(tx.symbol, "006208")
        self.assertEqual(tx.quantity, Decimal("2000"))
        self.assertEqual(tx.unit, "SHARE")
        self.assertEqual(tx.currency, "TWD")
        self.assertEqual(tx.price, Decimal("55.30"))

    def test_us_trade_infers_usd(self):
        tx = parse_compact_transaction("賣出 QQQM 3 股，價格 @ 180")
        self.assertEqual(tx.action, Action.SELL)
        self.assertEqual(tx.asset_type, "美股")
        self.assertEqual(tx.currency, "USD")
        self.assertEqual(tx.quantity, Decimal("3"))

    def test_cash_deposit_uses_currency_as_unit(self):
        tx = parse_compact_transaction("存入 100,000 TWD")
        self.assertEqual(tx.action, Action.DEPOSIT)
        self.assertEqual(tx.asset_type, "現金_TWD")
        self.assertEqual(tx.symbol, "TWD")
        self.assertEqual(tx.quantity, Decimal("100000"))
        self.assertEqual(tx.unit, "TWD")

    def test_ambiguous_trade_fails_closed(self):
        with self.assertRaises(ValueError):
            parse_compact_transaction("買入 006208 2 55.3")

    def test_missing_action_fails_closed(self):
        with self.assertRaises(ValueError):
            parse_compact_transaction("006208 2 張")

    def test_compact_form_rows_create_internal_metadata(self):
        headers = ["Timestamp", "Email Address", "交易內容", "交易日期", "價格／匯率", "備註"]
        rows = [["2026-08-05T14:45:00+08:00", "owner@example.com", "買入 006208 2 張，價格 55.30", "", "", ""]]
        result = parse_transaction_rows(headers, rows, source_sheet="Form")
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(result.accepted[0].transaction_date.isoformat(), "2026-08-05")
        self.assertEqual(result.accepted[0].quantity, Decimal("2000"))
        self.assertEqual(len(result.accepted[0].transaction_id), 36)
        self.assertEqual(result.accepted_rows[0][1:4], ("台股", "006208", "買入"))

    def test_compact_form_can_keep_review_gate(self):
        headers = ["Timestamp", "Email Address", "交易內容", "approved"]
        rows = [["2026-08-05T14:45:00+08:00", "owner@example.com", "存入 1000 TWD", "false"]]
        result = parse_transaction_rows(headers, rows, source_sheet="Form")
        self.assertEqual(len(result.accepted), 0)
        self.assertEqual(len(result.pending), 1)

    def test_dividend_is_recorded_as_cash_in_legacy_snapshot(self):
        headers = ["Timestamp", "Email Address", "交易內容"]
        rows = [["2026-08-05T14:45:00+08:00", "owner@example.com", "2330 股息 1200 TWD"]]
        result = parse_transaction_rows(headers, rows, source_sheet="Form")
        self.assertEqual(result.accepted_rows[0][1:4], ("現金_TWD", "TWD", "存入"))

    def test_compact_parser_preserves_old_form_rows(self):
        headers = [
            "Timestamp", "Email Address", "approved", "currency", "unit",
            "資產類別", "資產代號", "交易類型", "數量/股數/金額 (直接填正數即可)",
            "備註", "交易內容", "交易日期", "價格／匯率",
        ]
        rows = [
            ["2026-08-04T14:45:00+08:00", "owner@example.com", "true", "TWD", "張", "台股", "006208", "買入 / 存入 (+)", "2", "", "", "", ""],
            ["2026-08-05T14:45:00+08:00", "owner@example.com", "", "", "", "", "", "", "", "", "存入 1000 TWD", "", ""],
        ]
        result = parse_transaction_rows(headers, rows, source_sheet="Form")
        self.assertEqual(len(result.accepted), 2)
        self.assertEqual(result.accepted[0].symbol, "006208")
        self.assertEqual(result.accepted[1].symbol, "TWD")


if __name__ == "__main__":
    unittest.main()
