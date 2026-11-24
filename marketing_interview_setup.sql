-- Apply only to an approved environment after review.
BEGIN;
CREATE TABLE IF NOT EXISTS public.marketing_interviews (
    owner_id bigint NOT NULL,
    project_id uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    encrypted_state text NOT NULL CHECK (length(encrypted_state) BETWEEN 1 AND 1500000),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, project_id),
    FOREIGN KEY (project_id, owner_id) REFERENCES public.content_factory_projects (id, owner_id)
);
ALTER TABLE public.marketing_interviews ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.marketing_interviews FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON public.marketing_interviews TO service_role;
CREATE TABLE IF NOT EXISTS public.marketing_passport_versions (
    owner_id bigint NOT NULL,
    project_id uuid NOT NULL,
    version bigint NOT NULL CHECK (version > 0),
    restored_from bigint CHECK (restored_from < version),
    encrypted_passport text NOT NULL CHECK (length(encrypted_passport) BETWEEN 1 AND 1500000),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, project_id, version),
    FOREIGN KEY (project_id, owner_id) REFERENCES public.content_factory_projects (id, owner_id),
    FOREIGN KEY (owner_id, project_id, restored_from)
        REFERENCES public.marketing_passport_versions (owner_id, project_id, version)
);
ALTER TABLE public.marketing_passport_versions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.marketing_passport_versions FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT ON public.marketing_passport_versions TO service_role;
COMMIT;
