import unittest

from transaction_schema import (
    CURRENT_FORM_SCHEMA,
    FORM_V2_SCHEMA,
    LEGACY_SCHEMA,
    UNKNOWN_SCHEMA,
    adapt_known_legacy_rows,
    detect_schema,
)


class SchemaBoundaryTests(unittest.TestCase):
    def test_current_three_question_form_is_explicit(self):
        headers = ["Timestamp", "Email Address", "交易類型", "交易單位", "交易數量"]
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


if __name__ == "__main__":
    unittest.main()
