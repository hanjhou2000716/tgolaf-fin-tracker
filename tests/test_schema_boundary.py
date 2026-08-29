import unittest
from decimal import Decimal

from transaction_schema import (
    CURRENT_FORM_SCHEMA,
    FORM_V2_SCHEMA,
    LEGACY_SCHEMA,
    UNKNOWN_SCHEMA,
    Action,
    adapt_known_legacy_rows,
    analyze_schema,
    detect_schema,
    parse_transaction_rows,
    schema_drift_digest,
)


class SchemaBoundaryTests(unittest.TestCase):
    def test_current_three_question_form_is_explicit(self):
        headers = ["Timestamp", "Email Address", "交易類型", "交易單位", "交易數量"]
        self.assertEqual(detect_schema(headers), CURRENT_FORM_SCHEMA)

    def test_current_three_question_form_without_email_transport_is_still_current(self):
        headers = ["Timestamp", "交易類型", "交易單位", "交易數量"]
        self.assertEqual(detect_schema(headers), CURRENT_FORM_SCHEMA)

    def test_form_v2_is_not_treated_as_unknown_legacy(self):
        headers = ["Timestamp", "Email Address", "交易類型", "市場", "資產代號", "數量", "單位", "價格", "金額", "幣別", "交易日期"]
        self.assertEqual(detect_schema(headers), FORM_V2_SCHEMA)

    def test_fixed_legacy_schema_is_header_mapped(self):
        headers = ["transaction_id", "Timestamp", "Email Address", "approved", "transaction_date", "asset_type", "symbol", "action", "quantity", "unit", "currency"]
        self.assertEqual(detect_schema(headers), LEGACY_SCHEMA)
        rows = [["id", "2026-08-20", "owner@example.com", "true", "2026-08-20", "台股", "006208", "買入", "1 張", "張", "TWD"]]
        self.assertEqual(adapt_known_legacy_rows(headers, rows)[0][1:], ("台股", "006208", "買入", "1 張"))

    def test_unknown_schema_never_gets_legacy_adapter(self):
        headers = ["Timestamp", "Email Address", "第 7 欄", "第 10 欄", "自由描述"]
        self.assertEqual(detect_schema(headers), UNKNOWN_SCHEMA)

    def test_reordered_current_headers_are_safe_and_same_shape(self):
        first = analyze_schema(["Timestamp", "Email Address", "交易類型", "交易單位", "交易數量"])
        reordered = analyze_schema(["交易數量", "Email Address", "交易單位", "交易類型", "Timestamp"])
        self.assertTrue(first["safe"])
        self.assertTrue(reordered["safe"])
        self.assertEqual(first["fingerprint"], reordered["fingerprint"])
        self.assertEqual(schema_drift_digest([first, reordered]), "")

    def test_known_alias_and_harmless_google_extra_are_safe(self):
        result = analyze_schema([
            "提交時間", "Email", "交易類型（標的＋動作）", "交易單位", "交易數量", "Response ID",
        ])
        self.assertTrue(result["safe"])
        self.assertEqual(result["missingFields"], [])
        self.assertEqual(result["ignoredExtraHeaders"], ["Response ID"])

    def test_missing_or_duplicate_required_header_is_quarantined(self):
        missing = analyze_schema(["Timestamp", "Email Address", "交易類型", "交易數量"])
        duplicate = analyze_schema([
            "Timestamp", "Email Address", "交易類型", "交易單位", "交易數量", "交易數量",
        ])
        self.assertFalse(missing["safe"])
        self.assertIn("unit", missing["missingFields"])
        self.assertFalse(duplicate["safe"])
        self.assertIn("quantity", duplicate["duplicateFields"])
        self.assertTrue(schema_drift_digest([missing]))

    def test_mixed_duplicate_headers_are_safe_when_rows_are_disjoint(self):
        headers = ["Timestamp", "Email Address", "交易類型", "交易單位", "交易數量", "交易數量"]
        rows = [
            ["2026-08-24T05:40:00+08:00", "owner@example.com", "006208 買入", "股", "100", ""],
            ["2026-08-24T05:41:00+08:00", "owner@example.com", "QQQM 賣出", "股", "", "3"],
        ]
        result = analyze_schema(headers, rows=rows)
        self.assertTrue(result["safe"])
        self.assertTrue(result["duplicateResolved"])
        self.assertEqual(schema_drift_digest([result]), "")

    def test_unknown_accounting_header_is_not_silently_accepted(self):
        result = analyze_schema([
            "Timestamp", "Email Address", "交易類型", "交易單位", "交易數量", "新交易金額欄位",
        ])
        self.assertFalse(result["safe"])
        self.assertEqual(result["reason"], "unknown_accounting_headers")

    def test_recovery_parser_accepts_known_rows_with_extra_accounting_header(self):
        headers = ["Timestamp", "Email Address", "交易類型", "交易單位", "交易數量", "新交易金額欄位"]
        rows = [["2026-08-24T05:40:00+08:00", "owner@example.com", "006208 買入", "股", "100", ""]]
        parsed = parse_transaction_rows(headers, rows, source_sheet="回覆", existing_ids=set())
        self.assertEqual(len(parsed.accepted), 1)
        self.assertFalse(parsed.rejected)

    def test_duplicate_candidates_with_two_values_are_not_recovery_safe(self):
        headers = ["Timestamp", "Email Address", "交易類型", "交易單位", "交易數量", "交易數量"]
        rows = [["2026-08-24T05:40:00+08:00", "owner@example.com", "006208 買入", "股", "100", "101"]]
        result = analyze_schema(headers, rows=rows)
        self.assertFalse(result["duplicateResolved"])
        self.assertFalse(result["safe"])

    def test_mixed_current_and_legacy_rows_are_parsed_by_their_populated_branch(self):
        headers = [
            "Timestamp", "Email Address", "交易類型", "交易單位", "交易數量",
            "asset_type", "symbol", "action", "quantity", "market", "unit",
            "currency", "target_balance", "transaction_date",
        ]
        rows = [
            [
                "2026-08-24T05:40:00+08:00", "owner@example.com", "006208 買入", "股", "100",
                "", "", "", "", "", "", "", "", "",
            ],
            [
                "2026-08-23T14:45:00+08:00", "", "", "", "",
                "台股", "006208", "買入", "100", "", "股", "TWD", "", "",
            ],
        ]
        result = parse_transaction_rows(headers, rows, source_sheet="表單回覆 3")
        self.assertEqual(result.rejected, ())
        self.assertEqual(len(result.accepted), 2)
        self.assertEqual([item.source_row_id for item in result.accepted], ["表單回覆 3:2", "表單回覆 3:3"])
        self.assertEqual([item.action for item in result.accepted], [Action.BUY, Action.BUY])

    def test_production_three_question_rows_without_email_recover_by_explicit_flag(self):
        headers = [
            "時間戳記", "資產類別", "資產代號", "交易類型", "數量/股數/金額 (直接填正數即可)",
            "市場", "approved", "第 7 欄", "unit", "currency", "目標餘額", "交易日期", "幣別",
            "交易類型", "備註", "資產代號", "單位", "幣別", "備註", "數量", "價格", "金額",
            "幣別", "交易日期", "備註", "金額", "幣別", "交易日期", "備註", "交易單位", "交易數量", "第 10 欄",
        ]
        buy = [""] * len(headers)
        buy[0] = "2026/8/29 下午 8:55:24"
        buy[13], buy[29], buy[30] = "006208 買入", "股", "300"
        cash = [""] * len(headers)
        cash[0] = "2026/8/29 下午 8:56:56"
        cash[13], cash[29], cash[30] = "現金 取代", "台幣", "78000"
        result = parse_transaction_rows(
            headers, [buy, cash], source_sheet="表單回覆 3",
            allow_missing_email_compat=True,
        )
        self.assertEqual(result.rejected, ())
        self.assertEqual([(item.action, item.symbol, item.quantity, item.unit) for item in result.accepted], [
            (Action.BUY, "006208", Decimal("300"), "SHARE"),
            (Action.SET_BALANCE, "TWD", Decimal("78000"), "TWD"),
        ])


if __name__ == "__main__":
    unittest.main()
