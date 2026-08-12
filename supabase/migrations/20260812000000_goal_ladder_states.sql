create table if not exists public.goal_ladder_states (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  state jsonb not null check (jsonb_typeof(state) = 'object'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint goal_ladder_states_user_id_key unique (user_id)
);

comment on table public.goal_ladder_states is
  'Private monotonic Growth goal achievements. Written only by the server-side sync job.';

alter table public.goal_ladder_states enable row level security;
revoke all on table public.goal_ladder_states from anon;
revoke all on table public.goal_ladder_states from authenticated;

create or replace function public.touch_goal_ladder_state_updated_at()
returns trigger language plpgsql security invoker as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists goal_ladder_states_touch_updated_at on public.goal_ladder_states;
create trigger goal_ladder_states_touch_updated_at
  before update on public.goal_ladder_states
  for each row execute function public.touch_goal_ladder_state_updated_at();
