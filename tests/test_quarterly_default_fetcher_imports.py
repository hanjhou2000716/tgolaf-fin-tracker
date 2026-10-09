import unittest

import beta_policy
import quarterly_risk_policy


class QuarterlyDefaultFetcherImports(unittest.TestCase):
    def test_cbc_fallback_used_by_default_fetcher_path_is_imported(self):
        self.assertIs(
            quarterly_risk_policy.fetch_cbc_usd_twd_series,
            beta_policy.fetch_cbc_usd_twd_series,
        )


if __name__ == "__main__":
    unittest.main()
