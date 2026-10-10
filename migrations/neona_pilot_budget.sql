-- Неона 4.0: общий расход пилота. Применять ТОЛЬКО после ревью.
-- Денежная единица: микро-USD. Лимит задаётся консервативно;
-- 30 EUR нельзя приравнивать к 30 USD без проверки курса.
create table if not exists public.neona_pilot_budget (
  pilot_key text primary key,
  limit_micro_usd bigint not null check (limit_micro_usd > 0),
  spent_micro_usd bigint not null default 0 check (spent_micro_usd >= 0),
  reserved_micro_usd bigint not null default 0 check (reserved_micro_usd >= 0),
  enabled boolean not null default false,
  ends_at timestamptz,
  check (spent_micro_usd + reserved_micro_usd <= limit_micro_usd)
);
create table if not exists public.neona_pilot_reservations (
  request_id uuid primary key,
  pilot_key text not null references public.neona_pilot_budget(pilot_key),
  owner_id bigint not null,
  reserved_micro_usd bigint not null check (reserved_micro_usd > 0),
  actual_micro_usd bigint,
  status text not null default 'reserved' check (status in ('reserved','settled','released')),
  created_at timestamptz not null default now()
);
alter table public.neona_pilot_budget enable row level security;
alter table public.neona_pilot_reservations enable row level security;
-- No client access policies. Server's service-role key only.

create or replace function public.neona_pilot_reserve(
  p_pilot_key text, p_request_id uuid, p_owner_id bigint, p_amount bigint
) returns boolean language plpgsql security definer
set search_path = public as $$
declare b public.neona_pilot_budget%rowtype;
begin
  if p_amount <= 0 then return false; end if;
  select * into b from public.neona_pilot_budget
    where pilot_key = p_pilot_key for update;
  if not found or not b.enabled or (b.ends_at is not null and now() >= b.ends_at)
     or b.spent_micro_usd + b.reserved_micro_usd + p_amount > b.limit_micro_usd
  then return false; end if;
  insert into public.neona_pilot_reservations(request_id,pilot_key,owner_id,reserved_micro_usd)
    values(p_request_id,p_pilot_key,p_owner_id,p_amount);
  update public.neona_pilot_budget set reserved_micro_usd=reserved_micro_usd+p_amount
    where pilot_key=p_pilot_key;
  return true;
end; $$;

create or replace function public.neona_pilot_settle(
  p_request_id uuid, p_actual_micro_usd bigint
) returns boolean language plpgsql security definer
set search_path = public as $$
declare r public.neona_pilot_reservations%rowtype;
begin
  select * into r from public.neona_pilot_reservations where request_id=p_request_id for update;
  if not found or r.status <> 'reserved' then return false; end if;
  -- Never undercount a higher-than-reserved bill: hold reserved amount
  -- and flag an overrun to the administrator.
  if p_actual_micro_usd < 0 or p_actual_micro_usd > r.reserved_micro_usd then
    return false;
  end if;
  update public.neona_pilot_reservations
  set status='settled',actual_micro_usd=p_actual_micro_usd where request_id=p_request_id;
  update public.neona_pilot_budget
  set reserved_micro_usd=reserved_micro_usd-r.reserved_micro_usd,
      spent_micro_usd=spent_micro_usd+p_actual_micro_usd
  where pilot_key=r.pilot_key;
  return true;
end; $$;

create or replace function public.neona_pilot_release(p_request_id uuid)
returns boolean language plpgsql security definer
set search_path = public as $$
declare r public.neona_pilot_reservations%rowtype;
begin
  select * into r from public.neona_pilot_reservations where request_id=p_request_id for update;
  if not found or r.status <> 'reserved' then return false; end if;
  update public.neona_pilot_reservations set status='released' where request_id=p_request_id;
  update public.neona_pilot_budget set reserved_micro_usd=reserved_micro_usd-r.reserved_micro_usd
   where pilot_key=r.pilot_key;
  return true;
end; $$;
-- CRITICAL: revoke RPC invocation from browser/public roles.
revoke all on function public.neona_pilot_reserve(text,uuid,bigint,bigint) from public, anon, authenticated;
revoke all on function public.neona_pilot_settle(uuid,bigint) from public, anon, authenticated;
revoke all on function public.neona_pilot_release(uuid) from public, anon, authenticated;
