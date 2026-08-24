import unittest

from transaction_schema import (
    CURRENT_FORM_SCHEMA,
    FORM_V2_SCHEMA,
    LEGACY_SCHEMA,
    UNKNOWN_SCHEMA,
    adapt_known_legacy_rows,
    analyze_schema,
    detect_schema,
    schema_drift_digest,
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


if __name__ == "__main__":
    unittest.main()
