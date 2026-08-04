import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SupabaseContractTests(unittest.TestCase):
    def test_rls_migration_is_user_scoped_and_denies_anon(self):
        migration = (ROOT / "supabase" / "migrations" / "20260804000000_portfolio_snapshots.sql").read_text(encoding="utf-8")
        self.assertIn("enable row level security", migration.lower())
        self.assertIn("auth.uid() = user_id", migration)
        self.assertIn("revoke all on table public.portfolio_snapshots from anon", migration.lower())
        self.assertIn("grant select on table public.portfolio_snapshots to authenticated", migration.lower())

    def test_edge_function_has_explicit_auth_and_origin_guards(self):
        function = (ROOT / "supabase" / "functions" / "portfolio-data" / "index.ts").read_text(encoding="utf-8")
        self.assertIn('return response(request, { error: "unauthorized" }, 401)', function)
        self.assertIn('auth.getUser(token)', function)
        self.assertIn('X-Telegram-Init-Data', function)
        self.assertIn('TELEGRAM_BOT_TOKEN', function)
        self.assertIn('verifyTelegramInitData', function)
        self.assertIn('TELEGRAM_ALLOWED_USER_ID', function)
        self.assertIn('allowedOrigins.includes(origin)', function)
        self.assertNotIn('Access-Control-Allow-Origin": "*"', function)

    def test_transaction_ledger_is_append_only_and_user_scoped(self):
        migration = (ROOT / "supabase" / "migrations" / "20260804010000_portfolio_transactions.sql").read_text(encoding="utf-8")
        self.assertIn("portfolio_transactions_user_transaction_key", migration)
        self.assertIn("enable row level security", migration.lower())
        self.assertIn("auth.uid() = user_id", migration)
        self.assertIn("revoke all on table public.portfolio_transactions from anon", migration.lower())
        self.assertNotIn("for insert", migration.lower())
        self.assertNotIn("for update", migration.lower())


if __name__ == "__main__":
    unittest.main()
