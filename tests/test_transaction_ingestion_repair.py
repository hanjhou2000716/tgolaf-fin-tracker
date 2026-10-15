import unittest
from datetime import date
from decimal import Decimal

from transaction_command import (
    apply_current_transactions,
    build_ingestion_contract,
    build_ingestion_status,
    build_partial_update_message,
    transaction_order_key,
)
from form_v3 import parse_form_v3_rows
from ledger import transaction_payload
from settlement_pricing import enrich_missing_trade_prices
from supabase_sync import restore_canonical_ledger_transactions
from transaction_schema import Action, RejectedTransaction, Transaction


def tx(row, timestamp, symbol="006208"):
    return Transaction(
        transaction_id=f"id-{row}",
        source_row_id=f"CURRENT:{row}",
        submitted_at=timestamp,
        submitter_email="private@example.invalid",
        approved=True,
        transaction_date=date(2026, 10, 15),
        asset_type="台股",
        symbol=symbol,
        action=Action.BUY,
        quantity=Decimal("100"),
        unit="SHARE",
        currency="TWD",
    )


class TransactionIngestionRepairTests(unittest.TestCase):
    def test_row_8_is_added_once_and_row_9_replays_at_canonical_price(self):
        headers = ["Timestamp", "Email Address", "交易主體", "交易標的", "交易動作", "交易單位", "交易數量"]
        submitted = "2026-10-15T11:50:00+08:00"
        rows = [[submitted, "private@example.invalid", "台幣", "", "存入", "台幣", "1"] for _ in range(6)]
        rows.extend([
            [submitted, "private@example.invalid", "台股", "00685L", "買入", "張", "1"],
            [submitted, "private@example.invalid", "台股", "006208", "買入", "股", "100"],
        ])
        parsed = parse_form_v3_rows(headers, rows, source_sheet="TRANSACTIONS_CURRENT")
        self.assertEqual(parsed.rejected, ())
        self.assertEqual(parsed.pending, ())
        row_8 = next(item for item in parsed.accepted if item.source_row_id.endswith(":8"))
        row_9 = next(item for item in parsed.accepted if item.source_row_id.endswith(":9"))
        self.assertEqual((row_8.symbol, row_8.quantity, row_8.currency), ("00685L", Decimal("1000"), "TWD"))
        self.assertEqual((row_9.symbol, row_9.quantity), ("006208", Decimal("100")))

        canonical_row_9 = Transaction(**{
            **row_9.__dict__,
            "price": Decimal("59.5"),
            "compatibility_used": "settlement_quote_estimate:test:as_of=2026-10-15",
        })
        existing = [
            {"transaction_id": item.transaction_id, "source_row_id": item.source_row_id, "payload": transaction_payload(item)}
            for item in (*parsed.accepted[:6], canonical_row_9)
        ]
        restored, conflicts = restore_canonical_ledger_transactions(parsed.accepted, existing)
        self.assertEqual(conflicts, ())

        class ExactTradeDateQuote:
            price = "23.96"
            is_stale = False
            fallback_used = False
            source = "FinMind TaiwanStockPrice"
            as_of = "2026-10-15T00:00:00+08:00"
            evidence_hash = "b" * 64

        first = enrich_missing_trade_prices(
            restored,
            lambda transaction: ExactTradeDateQuote() if transaction.transaction_id == row_8.transaction_id else self.fail("canonical replay must not be repriced"),
        )
        self.assertEqual(first.pending, ())
        priced_row_8 = next(item for item in first.accepted if item.source_row_id.endswith(":8"))
        replayed_row_9 = next(item for item in first.accepted if item.source_row_id.endswith(":9"))
        self.assertEqual(priced_row_8.price, Decimal("23.96"))
        self.assertEqual(replayed_row_9.price, Decimal("59.5"))
        self.assertIn("evidence_sha256=" + "b" * 64, priced_row_8.compatibility_used)

        baseline = {"台股": {}, "現金_TWD": {"TWD": 100000}}
        first_inventory, _ = apply_current_transactions(baseline, first.accepted)
        first_cash = Decimal(str(baseline["現金_TWD"]["TWD"]))
        self.assertEqual(baseline["台股"], {"00685L": 1000, "006208": 100})
        self.assertEqual(first_cash, Decimal("70096.00"))

        complete_ledger = existing + [
            {"transaction_id": priced_row_8.transaction_id, "source_row_id": priced_row_8.source_row_id, "payload": transaction_payload(priced_row_8)}
        ]
        restored_again, replay_conflicts = restore_canonical_ledger_transactions(parsed.accepted, complete_ledger)
        self.assertEqual(replay_conflicts, ())
        second = enrich_missing_trade_prices(restored_again, lambda _: self.fail("completed replay must not request a new price"))
        second_baseline = {"台股": {}, "現金_TWD": {"TWD": 100000}}
        apply_current_transactions(second_baseline, second.accepted)
        self.assertEqual(second_baseline, baseline)

    def test_reducer_order_uses_timestamp_then_numeric_form_row(self):
        rows = [
            tx(10, "2026/10/15 上午 11:50:12"),
            tx(2, "2026/10/15 上午 11:50:12"),
            tx(3, "2026/10/15 下午 12:01:00"),
        ]
        self.assertEqual([item.source_row_id for item in sorted(rows, key=transaction_order_key)], [
            "CURRENT:2", "CURRENT:10", "CURRENT:3",
        ])

    def test_reducer_order_normalizes_taiwan_local_and_utc_timestamps(self):
        local = tx(2, "2026/10/15 下午 12:01:00")
        utc = tx(3, "2026-10-15T04:01:01Z")
        self.assertEqual([item.source_row_id for item in sorted([utc, local], key=transaction_order_key)], [
            "CURRENT:2", "CURRENT:3",
        ])

    def test_reducer_order_supports_google_sheets_month_first_timestamps(self):
        morning = tx(2, "10/15/2026 11:50:12 AM")
        afternoon = tx(3, "10/15/2026 12:01:00 PM")
        self.assertLess(transaction_order_key(morning)[0], transaction_order_key(afternoon)[0])

    def test_current_summary_is_complete_and_legacy_rows_are_separate(self):
        rows = build_ingestion_status(
            accepted=[tx(2, "2026-10-15T11:50:00+08:00")],
            rejected=[RejectedTransaction("CURRENT:8", "INVALID_SYMBOL", "unsupported symbol", "00685L", stage="symbol")],
            source_role="CURRENT",
        )
        rows += build_ingestion_status(
            rejected=[RejectedTransaction("ARCHIVE:4", "INVALID_EMAIL", "old archive row")],
            source_role="LEGACY_ARCHIVE",
        )
        contract = build_ingestion_contract(rows)
        self.assertEqual(contract["summary"], {"applied": 1, "pending": 0, "rejected": 1})
        self.assertEqual(contract["legacySummary"]["rejected"], 1)
        self.assertEqual(len(contract["issues"]), 1)
        self.assertEqual(contract["issues"][0]["sourceRowId"], "CURRENT:8")

    def test_partial_message_contains_reason_and_never_pnl(self):
        contract = {
            "issues": [{"sourceRowId": "CURRENT:8", "symbol": "00685L", "reasonCode": "PRICE_PENDING"}],
            "ingestionHealth": {"reasonCode": "TRANSACTION_PENDING"},
        }
        message = build_partial_update_message(contract)
        self.assertIn("Growth 資料部分更新", message)
        self.assertIn("00685L（第 8 列）", message)
        self.assertIn("PRICE_PENDING", message)
        self.assertIn("損益待交易資料完整後確認", message)
        self.assertNotIn("元", message)

    def test_trade_date_quote_pending_has_safe_specific_reason(self):
        pending = Transaction(**{
            **tx(8, "2026-10-15T11:50:00+08:00", "00685L").__dict__,
            "compatibility_used": "settlement_quote_pending:ValueError",
        })
        statuses = build_ingestion_status(pending=[pending])
        contract = build_ingestion_contract(statuses)
        message = build_partial_update_message(contract)
        self.assertEqual(contract["issues"][0]["reasonCode"], "TRADE_DATE_QUOTE_UNAVAILABLE")
        self.assertIn("交易日期收盤價尚無法驗證", message)


if __name__ == "__main__":
    unittest.main()
