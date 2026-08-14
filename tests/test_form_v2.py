import unittest
from decimal import Decimal

from transaction_command import CommandStatus, command_from_transaction
from transaction_schema import Action, parse_transaction_rows


HEADERS = [
    "Timestamp", "Email Address", "交易類型", "目標餘額", "市場", "資產代號",
    "數量", "單位", "價格", "金額", "幣別", "交易日期", "備註",
]


class FormV2Tests(unittest.TestCase):
    def test_mixed_response_sheet_keeps_exact_legacy_cash_snapshot(self):
        headers = [
            "時間戳記", "資產類別", "資產代號", "交易類型 ",
            "數量/股數/金額 (直接填正數即可)", "市場", "approved", "第 7 欄",
            "unit", "currency", "目標餘額", "交易日期", "幣別", "交易類型", "備註",
        ]
        row = ["2026/8/13 下午 3:08:19", "", "", "", "", "", "", "", "", "", "取代台幣現金金額", "", "150000", "", ""]
        result = parse_transaction_rows(headers, [row], source_sheet="表單回覆 3")
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(result.accepted[0].action, Action.SET_BALANCE)
        self.assertEqual(result.accepted[0].quantity, Decimal("150000"))
        status = command_from_transaction(result.accepted[0])
        self.assertEqual(status.status, CommandStatus.APPLIED_WITH_COMPATIBILITY)
        self.assertEqual(status.compatibility_used, "legacy_target_from_price_field")

    def test_set_balance_uses_target_balance_and_is_idempotent_by_source_row(self):
        row = [
            "2026-08-14T14:45:00+08:00", "owner@example.com", "現金餘額校正",
            "150000", "", "", "", "", "", "", "TWD", "2026-08-14", "月結校正",
        ]
        result = parse_transaction_rows(HEADERS, [row], source_sheet="Form V2")
        self.assertEqual(len(result.accepted), 1)
        tx = result.accepted[0]
        self.assertEqual(tx.action, Action.SET_BALANCE)
        self.assertEqual(tx.quantity, Decimal("150000"))
        self.assertEqual(tx.asset_type, "現金_TWD")
        self.assertEqual(tx.symbol, "TWD")
        self.assertEqual(len(tx.transaction_id), 36)
        duplicate = parse_transaction_rows(HEADERS, [row], source_sheet="Form V2", existing_ids={tx.transaction_id})
        self.assertEqual(duplicate.rejected[0].detail, "duplicate_transaction_id")

    def test_stock_buy_converts_lots_to_shares(self):
        row = [
            "2026-08-14T14:45:00+08:00", "owner@example.com", "買入",
            "", "台股", "006208", "1", "張", "55.30", "", "TWD", "2026-08-14", "",
        ]
        result = parse_transaction_rows(HEADERS, [row], source_sheet="Form V2")
        tx = result.accepted[0]
        self.assertEqual(tx.action, Action.BUY)
        self.assertEqual(tx.asset_type, "現貨台股")
        self.assertEqual(tx.quantity, Decimal("1000"))
        self.assertEqual(tx.unit, "SHARE")
        self.assertEqual(tx.price, Decimal("55.30"))

    def test_cash_flow_requires_amount_and_maps_to_cash(self):
        row = [
            "2026-08-14T14:45:00+08:00", "owner@example.com", "存入",
            "", "", "", "", "", "", "100000", "TWD", "2026-08-14", "",
        ]
        result = parse_transaction_rows(HEADERS, [row], source_sheet="Form V2")
        self.assertEqual(result.accepted[0].action, Action.DEPOSIT)
        self.assertEqual(result.accepted[0].quantity, Decimal("100000"))
        self.assertEqual(result.accepted[0].asset_type, "現金_TWD")

    def test_missing_v2_value_is_rejected_with_reason(self):
        row = [
            "2026-08-14T14:45:00+08:00", "owner@example.com", "現金餘額校正",
            "", "", "", "", "", "", "", "TWD", "2026-08-14", "",
        ]
        result = parse_transaction_rows(HEADERS, [row], source_sheet="Form V2")
        self.assertEqual(len(result.accepted), 0)
        self.assertIn("target balance is required", result.rejected[0].detail)


if __name__ == "__main__":
    unittest.main()
