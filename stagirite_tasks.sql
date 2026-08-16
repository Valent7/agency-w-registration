-- Стагирит 1.0 — постоянная история поручений

create extension if not exists pgcrypto;

create table if not exists public.agency_stagirite_tasks (
    id uuid primary key default gen_random_uuid(),
    owner_telegram_id bigint not null,
    assignment text not null,
    task_kind text not null default 'general',
    status text not null default 'planned',
    plan jsonb not null default '{}'::jsonb,
    result jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists agency_stagirite_tasks_owner_created_idx
    on public.agency_stagirite_tasks (owner_telegram_id, created_at desc);
