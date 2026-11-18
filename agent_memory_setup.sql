-- PREPARATION ONLY. Never applied automatically. Requires separate owner install approval.
-- Additive to content_factory_setup.sql. Keep legacy chat, notes, projects and jobs.
BEGIN;
CREATE TABLE IF NOT EXISTS public.agent_memory_workspaces (
    owner_id bigint NOT NULL CHECK(owner_id > 0),
    agent_id text NOT NULL CHECK(agent_id ~ '^[a-zA-Z0-9_*.-]{1,100}$'),
    workspace_id text NOT NULL CHECK(workspace_id ~ '^[a-zA-Z0-9_*.-]{1,100}$'),
    project_id uuid,
    revision bigint NOT NULL DEFAULT 0 CHECK(revision >= 0),
    PRIMARY KEY(owner_id, agent_id, workspace_id),
    FOREIGN KEY(project_id, owner_id) REFERENCES public.content_factory_projects(id,owner_id),
    CHECK(project_id IS NOT NULL OR workspace_id = 'owner'),
    CHECK(agent_id <> '*' OR project_id IS NULL)
);
CREATE TABLE IF NOT EXISTS public.agent_memory_revisions (
    owner_id bigint NOT NULL,
    agent_id text NOT NULL,
    workspace_id text NOT NULL,
    revision bigint NOT NULL CHECK(revision > 0),
    document jsonb NOT NULL CHECK(jsonb_typeof(document) = 'object' AND document->>'schema' = '1'),
    event jsonb NOT NULL CHECK(jsonb_typeof(event) = 'object'),
    hash text NOT NULL CHECK(hash ~ '^[a-f0-9]{64}$'),
    idempotency_key text NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 250),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(owner_id, agent_id, workspace_id, revision),
    UNIQUE(owner_id, agent_id, workspace_id, idempotency_key),
    FOREIGN KEY(owner_id, agent_id, workspace_id)
        REFERENCES public.agent_memory_workspaces(owner_id,agent_id,workspace_id)
);
ALTER TABLE public.agent_memory_workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_memory_revisions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.agent_memory_workspaces,public.agent_memory_revisions FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE ON public.agent_memory_workspaces TO service_role;
GRANT SELECT,INSERT ON public.agent_memory_revisions TO service_role;

CREATE OR REPLACE FUNCTION public.agent_memory_immutable() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public,pg_temp AS $$
BEGIN RAISE EXCEPTION 'Immutable memory history'; END;
$$;
REVOKE ALL ON FUNCTION public.agent_memory_immutable() FROM PUBLIC,anon,authenticated;
DROP TRIGGER IF EXISTS agent_memory_immutable ON public.agent_memory_revisions;
CREATE TRIGGER agent_memory_immutable BEFORE UPDATE OR DELETE ON public.agent_memory_revisions
FOR EACH ROW EXECUTE FUNCTION public.agent_memory_immutable();

CREATE OR REPLACE FUNCTION public.agent_memory_commit(
    p_owner bigint, p_agent text, p_workspace text, p_project uuid,
    p_expected bigint, p_document jsonb, p_event jsonb, p_key text, p_hash text
) RETURNS SETOF public.agent_memory_revisions
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public,pg_temp AS $$
DECLARE w public.agent_memory_workspaces; prior public.agent_memory_revisions;
BEGIN
    -- Only trusted server role can execute. User identity must come from server auth,
    -- not caller-supplied browser/model IDs. RLS is defense-in-depth, not service-role auth.
    IF p_project IS NOT NULL AND NOT EXISTS(
        SELECT 1 FROM public.content_factory_projects WHERE id=p_project AND owner_id=p_owner
    ) THEN RAISE EXCEPTION 'Memory owner/project mismatch'; END IF;
    IF octet_length(p_document::text) > 2000000 THEN RAISE EXCEPTION 'Memory too large'; END IF;
    INSERT INTO public.agent_memory_workspaces(owner_id,agent_id,workspace_id,project_id)
        VALUES(p_owner,p_agent,p_workspace,p_project) ON CONFLICT DO NOTHING;
    SELECT * INTO w FROM public.agent_memory_workspaces
        WHERE owner_id=p_owner AND agent_id=p_agent AND workspace_id=p_workspace FOR UPDATE;
    IF w.project_id IS DISTINCT FROM p_project THEN RAISE EXCEPTION 'Memory workspace mismatch'; END IF;
    SELECT * INTO prior FROM public.agent_memory_revisions
        WHERE owner_id=p_owner AND agent_id=p_agent AND workspace_id=p_workspace AND idempotency_key=p_key;
    IF FOUND THEN
        IF prior.hash <> p_hash OR prior.document <> p_document THEN
            RAISE EXCEPTION 'Memory idempotency conflict';
        END IF;
        RETURN NEXT prior; RETURN;
    END IF;
    IF w.revision <> p_expected THEN RAISE EXCEPTION 'Memory revision conflict'; END IF;
    INSERT INTO public.agent_memory_revisions(owner_id,agent_id,workspace_id,revision,document,event,hash,idempotency_key)
        VALUES(p_owner,p_agent,p_workspace,p_expected+1,p_document,p_event,p_hash,p_key);
    UPDATE public.agent_memory_workspaces SET revision=p_expected+1
        WHERE owner_id=p_owner AND agent_id=p_agent AND workspace_id=p_workspace;
    RETURN QUERY SELECT * FROM public.agent_memory_revisions
        WHERE owner_id=p_owner AND agent_id=p_agent AND workspace_id=p_workspace AND revision=p_expected+1;
END;
$$;
REVOKE ALL ON FUNCTION public.agent_memory_commit(bigint,text,text,uuid,bigint,jsonb,jsonb,text,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.agent_memory_commit(bigint,text,text,uuid,bigint,jsonb,jsonb,text,text) TO service_role;
COMMIT;
