import unittest

from exposure import build_exposure_matrix


class ExposureTests(unittest.TestCase):
    def test_etf_lookthrough_adds_to_company_without_double_counting(self):
        matrix = build_exposure_matrix(
            {"NVDA": 100, "QQQM": 200, "006208": 700},
            total_asset=1000,
            etf_lookthrough={"QQQM": {"NVDA": 0.1}},
            metadata={
                "NVDA": {"market": "US", "currency": "USD", "issuer": "NVDA", "industry": "Semis", "country": "US"},
                "QQQM": {"market": "US", "currency": "USD", "issuer": "Invesco", "industry": "ETF", "country": "US"},
                "006208": {"market": "TW", "currency": "TWD", "issuer": "Fubon", "industry": "ETF", "country": "TW"},
            },
        )
        self.assertEqual(matrix["company"]["NVDA"]["value"], 120)
        self.assertEqual(matrix["company"]["NVDA"]["percent"], 12)
        self.assertEqual(matrix["market"]["US"]["value"], 300)
        self.assertEqual(matrix["issuer"]["Invesco"]["value"], 200)
        self.assertEqual(matrix["country"]["US"]["value"], 300)
        self.assertEqual(matrix["industry"]["ETF"]["value"], 900)

    def test_unknown_metadata_is_explicit(self):
        matrix = build_exposure_matrix({"FUND": 50}, total_asset=100)
        self.assertEqual(matrix["market"]["unknown"]["percent"], 50)
        self.assertEqual(matrix["currency"]["unknown"]["percent"], 50)
        self.assertIn("leverageProduct", matrix)


if __name__ == "__main__":
    unittest.main()
