import json
import os
import tempfile
import unittest
from unittest.mock import patch

import sheets_retry
from history_store import upsert_history_snapshot


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class FakeSheetsError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.response = FakeResponse(status_code)


class RetryTests(unittest.TestCase):
    def setUp(self):
        sheets_retry.reset_operation_summary()

    def test_transient_503_recovers_without_logging_values(self):
        calls = []

        def operation():
            calls.append(1)
            if len(calls) == 1:
                raise FakeSheetsError(503)
            return "ok"

        with patch.object(sheets_retry, "GoogleSheetsAPIError", FakeSheetsError):
            result = sheets_retry.retry_sheet_operation(
                "history.row_values", operation, sleep=lambda _: None
            )
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 2)
        summary = sheets_retry.operation_summary()
        self.assertTrue(summary["operations"][0]["recovered"])
        self.assertNotIn("secret", json.dumps(summary))

    def test_non_transient_error_fails_fast(self):
        calls = []

        def operation():
            calls.append(1)
            raise FakeSheetsError(403)

        with patch.object(sheets_retry, "GoogleSheetsAPIError", FakeSheetsError):
            with self.assertRaises(FakeSheetsError):
                sheets_retry.retry_sheet_operation(
                    "history.get_all_values", operation, sleep=lambda _: None
                )
        self.assertEqual(len(calls), 1)

    def test_exhausted_transient_writes_private_non_financial_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "summary.json")
            with patch.dict(os.environ, {"SHEETS_OPERATION_SUMMARY_PATH": path}):
                with patch.object(sheets_retry, "GoogleSheetsAPIError", FakeSheetsError):
                    with self.assertRaises(FakeSheetsError):
                        sheets_retry.retry_sheet_operation(
                            "history.row_values", lambda: (_ for _ in ()).throw(FakeSheetsError(503)),
                            sleep=lambda _: None,
                        )
            with open(path, encoding="utf-8") as file:
                payload = json.load(file)
            self.assertEqual(payload["status"], "FAILED")
            self.assertEqual(payload["finalStatus"], 503)
            self.assertNotIn("secret", json.dumps(payload))


class IdempotentHistoryTests(unittest.TestCase):
    def test_append_response_loss_is_not_duplicated(self):
        class Sheet:
            def __init__(self):
                self.rows = [["Date", "Total_Asset"]]
                self.append_calls = 0

            def row_values(self, row):
                return list(self.rows[row - 1]) if row <= len(self.rows) else []

            def get_all_values(self):
                return [list(row) for row in self.rows]

            def update_cell(self, row, col, value):
                while len(self.rows) < row:
                    self.rows.append([])
                while len(self.rows[row - 1]) < col:
                    self.rows[row - 1].append("")
                self.rows[row - 1][col - 1] = value

            def update(self, range_name, values):
                row = int("".join(char for char in range_name if char.isdigit()))
                col = ord(range_name[0].upper()) - 64
                while len(self.rows[row - 1]) < col:
                    self.rows[row - 1].append("")
                self.rows[row - 1][col - 1] = values[0][0]

            def append_row(self, row):
                self.append_calls += 1
                self.rows.append(list(row))
                raise FakeSheetsError(503)

        sheet = Sheet()
        with patch.object(sheets_retry, "GoogleSheetsAPIError", FakeSheetsError):
            result = upsert_history_snapshot(
                sheet,
                {"Date": "2026-09-10", "Total_Asset": 123},
                sleep=lambda _: None,
            )
        self.assertEqual(result, "updated")
        self.assertEqual(sheet.append_calls, 1)
        self.assertEqual(sum(row[0] == "2026-09-10" for row in sheet.rows), 1)


if __name__ == "__main__":
    unittest.main()
