import os
import unittest
from unittest.mock import patch

from source_roles import SourceRoleConfig


class SourceRoleTests(unittest.TestCase):
    def test_roles_are_exact_not_fuzzy(self):
        config = SourceRoleConfig()
        self.assertEqual(config.role_for("TRANSACTIONS_CURRENT"), "CURRENT")
        self.assertEqual(config.role_for("表單回覆 3"), "LEGACY_ARCHIVE")
        self.assertEqual(config.role_for("History"), "HISTORY")
        self.assertIsNone(config.role_for("new form response"))
        self.assertIsNone(config.role_for("我的異動資料"))

    def test_environment_overrides_are_explicit_lists(self):
        with patch.dict(os.environ, {
            "CURRENT_TRANSACTION_SOURCE": "Current V3",
            "LEGACY_TRANSACTION_SOURCES": "old, archive,old",
            "HISTORY_SOURCE": "Ledger History",
        }, clear=False):
            config = SourceRoleConfig.from_environment()
        self.assertEqual(config.current, ("Current V3",))
        self.assertEqual(config.legacy, ("old", "archive"))
        self.assertEqual(config.history, ("Ledger History",))


if __name__ == "__main__":
    unittest.main()
