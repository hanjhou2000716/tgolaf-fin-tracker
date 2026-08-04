create table if not exists public.portfolio_transactions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  transaction_id uuid not null,
  source_row_id text not null,
  reversal_of uuid,
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  created_at timestamptz not null default now(),
  constraint portfolio_transactions_user_transaction_key unique (user_id, transaction_id)
);

comment on table public.portfolio_transactions is
  'Private append-only transaction ledger. Corrections are represented by REVERSAL entries.';

alter table public.portfolio_transactions enable row level security;
revoke all on table public.portfolio_transactions from anon;
revoke all on table public.portfolio_transactions from authenticated;
grant select on table public.portfolio_transactions to authenticated;

drop policy if exists "users can read their own portfolio transactions" on public.portfolio_transactions;
create policy "users can read their own portfolio transactions"
  on public.portfolio_transactions
  for select
  to authenticated
  using (auth.uid() = user_id);

create index if not exists portfolio_transactions_user_date_idx
  on public.portfolio_transactions (user_id, created_at);
