import unittest
from decimal import Decimal

from transaction_schema import Action, parse_transaction_rows


HEADERS = ["Timestamp", "Email Address", "交易類型", "交易單位", "交易數量"]


def row(transaction_type, unit, quantity, *, timestamp="2026-08-15T14:45:00+08:00", email="owner@example.com"):
    return [timestamp, email, transaction_type, unit, quantity]


class SimpleTransactionFormTests(unittest.TestCase):
    def parse(self, values):
        result = parse_transaction_rows(HEADERS, [values], source_sheet="Simple Form")
        self.assertEqual(result.rejected, ())
        self.assertEqual(result.pending, ())
        self.assertEqual(len(result.accepted), 1)
        return result.accepted[0], result.accepted_rows[0]

    def test_taiwan_lot_buy_is_normalized(self):
        transaction, legacy_row = self.parse(row("006208 買入", "張", "1"))
        self.assertEqual(transaction.action, Action.BUY)
        self.assertEqual(transaction.asset_type, "台股")
        self.assertEqual(transaction.symbol, "006208")
        self.assertEqual(transaction.quantity, Decimal("1000"))
        self.assertEqual(transaction.unit, "SHARE")
        self.assertEqual(transaction.currency, "TWD")
        self.assertIsNone(transaction.price)
        self.assertEqual(legacy_row[1:4], ("台股", "006208", "買入"))

    def test_us_sell_uses_usd_and_shares(self):
        transaction, _ = self.parse(row("QQQM 賣出", "股", "3"))
        self.assertEqual(transaction.action, Action.SELL)
        self.assertEqual(transaction.asset_type, "美股")
        self.assertEqual(transaction.currency, "USD")
        self.assertEqual(transaction.quantity, Decimal("3"))

    def test_cash_deposit_and_pledge_borrow(self):
        cash, _ = self.parse(row("現金 存入", "台幣", "100000"))
        pledge, _ = self.parse(row("質押 借款", "台幣", "1870000"))
        self.assertEqual((cash.action, cash.asset_type, cash.symbol), (Action.DEPOSIT, "現金_TWD", "TWD"))
        self.assertEqual((pledge.action, pledge.asset_type, pledge.symbol), (Action.BORROW, "質押負債", "Current_Debt"))

    def test_pledge_rate_keeps_percent_as_rate(self):
        transaction, legacy_row = self.parse(row("質押 利率", "%", "2.25"))
        self.assertEqual(transaction.action, Action.SET_PLEDGE_RATE)
        self.assertEqual(transaction.asset_type, "質押利率")
        self.assertEqual(transaction.symbol, "Rate")
        self.assertEqual(transaction.quantity, Decimal("2.25"))
        self.assertEqual(transaction.unit, "PERCENT")
        self.assertEqual(legacy_row[1:4], ("質押利率", "Rate", "取代"))

    def test_replace_supports_non_cash_assets(self):
        transaction, legacy_row = self.parse(row("006208 取代", "股", "2000"))
        self.assertEqual(transaction.action, Action.SET_BALANCE)
        self.assertEqual(transaction.quantity, Decimal("2000"))
        self.assertEqual(legacy_row[1:4], ("台股", "006208", "取代"))

    def test_invalid_unit_combination_is_rejected(self):
        result = parse_transaction_rows(HEADERS, [row("006208 買入", "台幣", "1000")], source_sheet="Simple Form")
        self.assertEqual(result.accepted, ())
        self.assertIn("股票／基金交易單位", result.rejected[0].detail)

    def test_invalid_quantity_is_rejected_without_guessing(self):
        result = parse_transaction_rows(HEADERS, [row("現金 存入", "台幣", "NT$100")], source_sheet="Simple Form")
        self.assertEqual(result.accepted, ())
        self.assertIn("交易數量只能填數字", result.rejected[0].detail)

    def test_missing_action_or_subject_is_rejected(self):
        result = parse_transaction_rows(HEADERS, [row("賣出", "股", "1")], source_sheet="Simple Form")
        self.assertEqual(result.accepted, ())
        self.assertIn("標的 動作", result.rejected[0].detail)

    def test_replay_is_idempotent_by_source_row(self):
        first, _ = self.parse(row("現金 存入", "台幣", "100"))
        duplicate = parse_transaction_rows(
            HEADERS,
            [row("現金 存入", "台幣", "100")],
            source_sheet="Simple Form",
            existing_ids={first.transaction_id},
        )
        self.assertEqual(duplicate.accepted, ())
        self.assertEqual(duplicate.rejected[0].detail, "duplicate_transaction_id")

    def test_mixed_duplicate_quantity_headers_use_non_empty_candidate(self):
        headers = ["Timestamp", "Email Address", "交易類型", "交易單位", "交易數量", "交易數量"]
        rows = [
            row("006208 買入", "股", "100") + [""],
            row("QQQM 賣出", "股", "") + ["3"],
        ]
        result = parse_transaction_rows(headers, rows, source_sheet="Simple Form")
        self.assertEqual(result.rejected, ())
        self.assertEqual([item.quantity for item in result.accepted], [Decimal("100"), Decimal("3")])

    def test_missing_email_requires_explicit_compatibility_recovery(self):
        headers = ["Timestamp", "交易類型", "交易單位", "交易數量"]
        values = ["2026-08-29T20:55:24+08:00", "006208 買入", "股", "300"]
        rejected = parse_transaction_rows(headers, [values], source_sheet="表單回覆 3")
        self.assertEqual(rejected.accepted, ())
        self.assertIn("submitter_email", rejected.rejected[0].detail)
        recovered = parse_transaction_rows(
            headers, [values], source_sheet="表單回覆 3",
            allow_missing_email_compat=True,
        )
        self.assertEqual(recovered.rejected, ())
        self.assertEqual(recovered.accepted[0].action, Action.BUY)
        self.assertEqual(recovered.accepted[0].symbol, "006208")
        self.assertEqual(recovered.accepted[0].quantity, Decimal("300"))
        self.assertEqual(recovered.accepted[0].unit, "SHARE")
        self.assertEqual(recovered.accepted[0].compatibility_used, "current_simple_form_missing_email")
        self.assertTrue(recovered.accepted[0].source_row_id.endswith("#current-simple-compat"))
        replay = parse_transaction_rows(
            headers, [values], source_sheet="表單回覆 3",
            existing_ids={recovered.accepted[0].transaction_id},
            allow_missing_email_compat=True,
        )
        self.assertEqual(replay.accepted, ())
        self.assertEqual(replay.rejected[0].detail, "duplicate_transaction_id")

    def test_missing_email_compatibility_recovers_cash_replacement(self):
        headers = ["Timestamp", "交易類型", "交易單位", "交易數量"]
        values = ["2026-08-29T20:56:56+08:00", "現金 取代", "台幣", "78000"]
        result = parse_transaction_rows(
            headers, [values], source_sheet="表單回覆 3",
            allow_missing_email_compat=True,
        )
        self.assertEqual(result.rejected, ())
        transaction = result.accepted[0]
        self.assertEqual(transaction.action, Action.SET_BALANCE)
        self.assertEqual(transaction.asset_type, "現金_TWD")
        self.assertEqual(transaction.symbol, "TWD")
        self.assertEqual(transaction.quantity, Decimal("78000"))
        self.assertEqual(transaction.unit, "TWD")


if __name__ == "__main__":
    unittest.main()
