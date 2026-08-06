-- Внутренний календарь встреч Агентства W
-- Выполнить один раз в Supabase → SQL Editor.

create extension if not exists pgcrypto;

create table if not exists public.agency_meetings (
    id uuid primary key default gen_random_uuid(),
    owner_telegram_id bigint not null,
    owner_name text,
    contact_telegram_id bigint,
    contact_name text not null,
    contact_username text,
    contact_city text,
    contact_timezone text not null default 'Europe/Moscow',
    start_at timestamptz not null,
    end_at timestamptz not null,
    meeting_format text not null,
    meeting_link text,
    status text not null default 'Ожидает подтверждения',
    notes text,
    source text not null default 'Внутренний календарь Агентства W',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint agency_meetings_time_order check (end_at > start_at),
    constraint agency_meetings_format_check check (
        meeting_format in ('Zoom', 'Telegram', 'WhatsApp')
    ),
    constraint agency_meetings_status_check check (
        status in (
            'Ожидает подтверждения',
            'Подтверждена',
            'Перенесена',
            'Отменена',
            'Состоялась'
        )
    )
);

create index if not exists agency_meetings_owner_start_idx
    on public.agency_meetings (owner_telegram_id, start_at);

create index if not exists agency_meetings_owner_status_idx
    on public.agency_meetings (owner_telegram_id, status);

alter table public.agency_meetings enable row level security;

-- Публичные политики намеренно не создаются.
-- Приложение обращается к таблице через SUPABASE_SECRET_KEY,
-- а владельцы кабинетов не получают прямого доступа к базе.
