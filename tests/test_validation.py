import unittest

from validation import validate_history_sheet, validate_inventory, validate_quote


def valid_inventory():
    return {
        "台股": {"2330": 100}, "美股": {"NVDA": 10}, "基金": {},
        "現金_TWD": {"TWD": 0}, "現金_USD": {"USD": 0},
        "質押負債": {"Current_Debt": 100}, "質押利率": {"Rate": 3.3},
        "擔保品": {"006208": 100},
    }


class FakeHistorySheet:
    def __init__(self, header):
        self.header = header

    def row_values(self, row):
        return self.header


class ValidationTests(unittest.TestCase):
    def test_accepts_complete_inventory(self):
        self.assertTrue(validate_inventory(valid_inventory()))

    def test_rejects_negative_position(self):
        inventory = valid_inventory()
        inventory["台股"]["2330"] = -1
        with self.assertRaises(ValueError):
            validate_inventory(inventory)

    def test_rejects_invalid_rate_and_price(self):
        inventory = valid_inventory()
        inventory["質押利率"]["Rate"] = 31
        with self.assertRaises(ValueError):
            validate_inventory(inventory)
        with self.assertRaises(ValueError):
            validate_quote("2330", 0)

    def test_requires_history_schema(self):
        self.assertTrue(validate_history_sheet(FakeHistorySheet(["Date", "Total_Asset", "Net_Asset"])))
        with self.assertRaises(ValueError):
            validate_history_sheet(FakeHistorySheet(["Date", "Net_Asset"]))


if __name__ == "__main__":
    unittest.main()
