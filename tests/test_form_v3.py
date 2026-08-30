import unittest
from decimal import Decimal

from form_v3 import FORM_V3_SCHEMA, is_form_v3_headers, parse_form_v3_rows
from transaction_schema import Action, detect_schema, parse_transaction_rows


HEADERS = ["Timestamp", "Email Address", "交易主體", "交易標的", "交易動作", "交易單位", "交易數量"]


def row(subject, symbol, action, unit, quantity, *, timestamp="2026-08-30T14:45:00+08:00"):
    return [timestamp, "owner@example.com", subject, symbol, action, unit, quantity]


class FormV3Tests(unittest.TestCase):
    def parse(self, values):
        result = parse_transaction_rows(HEADERS, [values], source_sheet="TRANSACTIONS_CURRENT")
        self.assertEqual(result.pending, ())
        self.assertEqual(result.rejected, ())
        self.assertEqual(len(result.accepted), 1)
        return result.accepted[0]

    def test_v3_identity_ignores_transport_metadata_and_order(self):
        self.assertTrue(is_form_v3_headers(HEADERS))
        self.assertEqual(detect_schema(HEADERS), FORM_V3_SCHEMA)
        reordered = ["交易數量", "交易動作", "交易主體", "交易單位", "交易標的", "Timestamp"]
        self.assertTrue(is_form_v3_headers(reordered))
        self.assertEqual(detect_schema(reordered), FORM_V3_SCHEMA)

    def test_taiwan_buy_lot(self):
        tx = self.parse(row("台股", "006208", "買入", "張", "1"))
        self.assertEqual((tx.action, tx.asset_type, tx.symbol, tx.quantity, tx.unit, tx.currency),
                         (Action.BUY, "台股", "006208", Decimal("1000"), "SHARE", "TWD"))

    def test_us_fractional_and_replacement_zero(self):
        tx = self.parse(row("美股", "QQQM", "買入", "股", "12.5"))
        self.assertEqual((tx.asset_type, tx.currency, tx.quantity), ("美股", "USD", Decimal("12.5")))
        replaced = self.parse(row("美股", "NVDA", "全數取代", "股", "0"))
        self.assertEqual((replaced.action, replaced.quantity), (Action.SET_BALANCE, Decimal("0")))

    def test_cash_and_pledge_contract(self):
        cash = self.parse(row("台幣", "", "全數取代", "台幣", "78000"))
        self.assertEqual((cash.action, cash.asset_type, cash.symbol, cash.unit), (Action.SET_BALANCE, "現金_TWD", "TWD", "TWD"))
        debt = self.parse(row("質押", "", "借款", "台幣", "1870000"))
        self.assertEqual((debt.action, debt.asset_type, debt.symbol, debt.currency), (Action.BORROW, "質押負債", "Current_Debt", "TWD"))
        rate = self.parse(row("質押", "", "利率", "%", "2.25"))
        self.assertEqual((rate.action, rate.asset_type, rate.symbol, rate.quantity, rate.unit), (Action.SET_PLEDGE_RATE, "質押利率", "Rate", Decimal("2.25"), "PERCENT"))

    def test_collateral_is_not_pledge(self):
        tx = self.parse(row("擔保品", "006208", "存入", "張", "1"))
        self.assertEqual((tx.asset_type, tx.symbol, tx.quantity, tx.action), ("擔保品", "006208", Decimal("1000"), Action.DEPOSIT))

    def test_invalid_matrix_is_fail_closed(self):
        cases = [
            row("台股", "", "買入", "股", "1"),
            row("台幣", "006208", "存入", "台幣", "100"),
            row("台股", "006208", "提領", "股", "1"),
            row("質押", "", "借款", "美金", "100"),
            row("擔保品", "006208", "借款", "股", "100"),
            row("台股", "006208", "買入", "股", "abc"),
        ]
        result = parse_form_v3_rows(HEADERS, cases, source_sheet="TRANSACTIONS_CURRENT")
        self.assertEqual(result.accepted, ())
        self.assertEqual(len(result.rejected), len(cases))

    def test_duplicate_business_column_only_accepts_one_non_empty_candidate(self):
        headers = HEADERS + ["交易數量"]
        good = row("台股", "006208", "買入", "股", "100") + [""]
        bad = row("台股", "006208", "買入", "股", "100") + ["101"]
        result = parse_form_v3_rows(headers, [good, bad], source_sheet="TRANSACTIONS_CURRENT")
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(result.accepted[0].quantity, Decimal("100"))
        self.assertEqual(result.rejected[0].reason, "DUPLICATE_QUANTITY_AMBIGUOUS")

    def test_idempotency_by_source_row(self):
        tx = self.parse(row("台股", "006208", "買入", "股", "100"))
        result = parse_form_v3_rows(HEADERS, [row("台股", "006208", "買入", "股", "100")], source_sheet="TRANSACTIONS_CURRENT", existing_ids={tx.transaction_id})
        self.assertEqual(result.accepted, ())
        self.assertEqual(result.rejected[0].reason, "DUPLICATE_TRANSACTION_ID")


if __name__ == "__main__":
    unittest.main()
