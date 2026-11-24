begin;

create table if not exists public.neonia_vk_control_reservations (
    run_id text primary key check (run_id = 'vk-pilot-control-001'),
    owner_id bigint not null check (owner_id > 0),
    reserved_units integer not null check (reserved_units between 1 and 1000000),
    created_at timestamptz not null default now()
);

alter table public.neonia_vk_control_reservations enable row level security;
revoke all on table public.neonia_vk_control_reservations from public, anon, authenticated, service_role;
grant select, insert on table public.neonia_vk_control_reservations to service_role;

commit;
