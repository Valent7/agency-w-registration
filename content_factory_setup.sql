-- Agency W content factory: run once in the project's Supabase SQL editor.
-- Re-running is safe. The browser must never receive the service-role key.
-- No anonymous/authenticated table policies are created; the trusted server
-- scopes every operation to its authenticated Telegram owner.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Keep the existing manual content-package screen working as well.
CREATE TABLE IF NOT EXISTS public.agency_content_packages (
    package_id uuid PRIMARY KEY,
    owner_telegram_id bigint NOT NULL,
    project_name text NOT NULL DEFAULT '',
    week_title text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'draft',
    settings jsonb NOT NULL DEFAULT '{}'::jsonb,
    package jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agency_content_packages_owner_updated
    ON public.agency_content_packages(owner_telegram_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS public.content_factory_projects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id bigint NOT NULL CHECK (owner_id > 0),
    automation_enabled boolean NOT NULL DEFAULT false,
    data jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(id, owner_id)
);

CREATE TABLE IF NOT EXISTS public.content_factory_connections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id bigint NOT NULL,
    project_id uuid NOT NULL,
    platform text NOT NULL,
    account_id text NOT NULL,
    display_name text NOT NULL DEFAULT '',
    token_encrypted text NOT NULL,
    extra jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(owner_id, project_id, platform),
    FOREIGN KEY(project_id, owner_id)
        REFERENCES public.content_factory_projects(id, owner_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS public.content_factory_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id bigint NOT NULL,
    project_id uuid NOT NULL,
    dedupe_key text NOT NULL,
    platform text NOT NULL,
    format text NOT NULL,
    state text NOT NULL DEFAULT 'queued',
    scheduled_at timestamptz NOT NULL,
    available_at timestamptz NOT NULL,
    lease_owner text,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    data jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(owner_id, project_id, dedupe_key),
    FOREIGN KEY(project_id, owner_id)
        REFERENCES public.content_factory_projects(id, owner_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS content_factory_jobs_due
    ON public.content_factory_jobs(state, available_at, lease_until);
CREATE INDEX IF NOT EXISTS content_factory_jobs_owner_schedule
    ON public.content_factory_jobs(owner_id, project_id, scheduled_at DESC);

CREATE TABLE IF NOT EXISTS public.content_factory_generation_reservations (
    owner_id bigint NOT NULL,
    project_id uuid NOT NULL,
    reservation_id text NOT NULL,
    day date NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(owner_id, project_id, reservation_id),
    FOREIGN KEY(project_id, owner_id)
        REFERENCES public.content_factory_projects(id, owner_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS content_factory_generation_reservations_day
    ON public.content_factory_generation_reservations(owner_id, project_id, day);

ALTER TABLE public.agency_content_packages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.content_factory_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.content_factory_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.content_factory_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.content_factory_generation_reservations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.agency_content_packages, public.content_factory_projects,
    public.content_factory_connections, public.content_factory_jobs,
    public.content_factory_generation_reservations FROM anon, authenticated;
GRANT ALL ON public.agency_content_packages, public.content_factory_projects,
    public.content_factory_connections, public.content_factory_jobs,
    public.content_factory_generation_reservations TO service_role;

-- Global claims are service-only. The UI supplies both owner and project.
CREATE OR REPLACE FUNCTION public.content_factory_claim_job(
    p_worker_id text,
    p_now timestamptz,
    p_lease_seconds integer DEFAULT 900,
    p_owner_id bigint DEFAULT NULL,
    p_project_id uuid DEFAULT NULL
) RETURNS SETOF public.content_factory_jobs
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    selected_id uuid;
BEGIN
    IF p_worker_id IS NULL OR length(p_worker_id) NOT BETWEEN 1 AND 200
       OR p_now IS NULL OR p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 1 AND 7200 THEN
        RAISE EXCEPTION 'Invalid worker claim parameters';
    END IF;

    SELECT j.id INTO selected_id
    FROM public.content_factory_jobs j
    WHERE j.state IN ('queued', 'scheduled')
      AND j.available_at <= p_now
      AND (j.lease_until IS NULL OR j.lease_until <= p_now)
      AND (p_owner_id IS NULL OR j.owner_id = p_owner_id)
      AND (p_project_id IS NULL OR j.project_id = p_project_id)
    ORDER BY j.available_at, j.created_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED;

    IF selected_id IS NULL THEN
        RETURN;
    END IF;
    RETURN QUERY UPDATE public.content_factory_jobs
    SET lease_owner = p_worker_id,
        lease_until = p_now + make_interval(secs => p_lease_seconds),
        attempts = attempts + 1,
        updated_at = p_now
    WHERE id = selected_id
    RETURNING *;
END;
$$;
REVOKE ALL ON FUNCTION public.content_factory_claim_job(text, timestamptz, integer, bigint, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.content_factory_claim_job(text, timestamptz, integer, bigint, uuid)
    TO service_role;

-- Serialize reservations on the project row so concurrent workers cannot
-- exceed the daily paid-attempt allowance. Failed attempts still count.
CREATE OR REPLACE FUNCTION public.content_factory_reserve_generation(
    p_owner_id bigint,
    p_project_id uuid,
    p_reservation_id text,
    p_day date,
    p_limit integer
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    used_count integer;
BEGIN
    IF p_owner_id IS NULL OR p_owner_id <= 0 OR p_project_id IS NULL
       OR p_reservation_id IS NULL OR length(p_reservation_id) NOT BETWEEN 1 AND 300
       OR p_day IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'Invalid generation reservation parameters';
    END IF;
    PERFORM 1 FROM public.content_factory_projects
      WHERE id = p_project_id AND owner_id = p_owner_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Project not found for owner';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.content_factory_generation_reservations
        WHERE owner_id = p_owner_id AND project_id = p_project_id
          AND reservation_id = p_reservation_id
    ) THEN
        RETURN true;
    END IF;
    SELECT count(*) INTO used_count FROM public.content_factory_generation_reservations
      WHERE owner_id = p_owner_id AND project_id = p_project_id AND day = p_day;
    IF used_count >= p_limit THEN
        RETURN false;
    END IF;
    INSERT INTO public.content_factory_generation_reservations
      (owner_id, project_id, reservation_id, day)
      VALUES(p_owner_id, p_project_id, p_reservation_id, p_day);
    RETURN true;
END;
$$;
REVOKE ALL ON FUNCTION public.content_factory_reserve_generation(bigint, uuid, text, date, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.content_factory_reserve_generation(bigint, uuid, text, date, integer)
    TO service_role;

-- Draft media remain private. The worker signs short-lived download URLs
-- only when a social network needs to fetch the current publication asset.
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES ('content-factory-media', 'content-factory-media', false, 104857600,
    ARRAY['image/jpeg','image/png','image/webp','video/mp4','audio/mpeg'])
ON CONFLICT (id) DO UPDATE SET public = false;
