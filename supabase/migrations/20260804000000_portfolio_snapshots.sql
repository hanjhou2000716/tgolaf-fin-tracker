create extension if not exists pgcrypto;

create table if not exists public.portfolio_snapshots (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  generated_at timestamptz not null,
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint portfolio_snapshots_user_id_key unique (user_id)
);

comment on table public.portfolio_snapshots is
  'Private Growth portfolio snapshots. One current snapshot per authenticated user.';
comment on column public.portfolio_snapshots.payload is
  'Private data_for_web payload. Never publish this table to GitHub Pages.';

alter table public.portfolio_snapshots enable row level security;

revoke all on table public.portfolio_snapshots from anon;
revoke all on table public.portfolio_snapshots from authenticated;
grant select on table public.portfolio_snapshots to authenticated;

drop policy if exists "users can read their own portfolio snapshot" on public.portfolio_snapshots;
create policy "users can read their own portfolio snapshot"
  on public.portfolio_snapshots
  for select
  to authenticated
  using (auth.uid() = user_id);

create or replace function public.touch_portfolio_snapshot_updated_at()
returns trigger
language plpgsql
security invoker
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists portfolio_snapshots_touch_updated_at on public.portfolio_snapshots;
create trigger portfolio_snapshots_touch_updated_at
  before update on public.portfolio_snapshots
  for each row execute function public.touch_portfolio_snapshot_updated_at();
