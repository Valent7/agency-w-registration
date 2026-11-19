-- PREPARATION ONLY: additive control plane, not another memory store.
-- Never execute automatically or from an application startup.
BEGIN;
CREATE SCHEMA IF NOT EXISTS kadrian_sync_control;
REVOKE ALL ON SCHEMA kadrian_sync_control FROM PUBLIC,anon,authenticated,service_role;
CREATE TABLE IF NOT EXISTS kadrian_sync_control.gate (
 owner_id bigint NOT NULL, agent_id text NOT NULL, workspace_id text NOT NULL,
 project_id uuid NOT NULL, device_id text NOT NULL, release_id text NOT NULL CHECK(release_id ~ '^[a-f0-9]{32}$'),
 generation bigint NOT NULL DEFAULT 1 CHECK(generation>0),
 enabled boolean NOT NULL DEFAULT false, expires_at timestamptz,
 approval text, changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(owner_id,agent_id,workspace_id),
 FOREIGN KEY(project_id,owner_id) REFERENCES public.content_factory_projects(id,owner_id),
 CHECK(NOT enabled OR coalesce(length(approval)>0,false))
);
ALTER TABLE kadrian_sync_control.gate ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON kadrian_sync_control.gate FROM PUBLIC,anon,authenticated,service_role;

-- SECURITY DEFINER is necessary only to lock the private gate without granting
-- the runtime UPDATE permission. Fixed search_path, fully qualified objects,
-- explicit exact binding, no dynamic SQL and no gate mutation by this function.
CREATE OR REPLACE FUNCTION public.kadrian_memory_exchange(
 p_action text, p_release text, p_device text, p_owner bigint, p_agent text,
 p_workspace text, p_project uuid, p_generation bigint, p_scope jsonb,
 p_expected bigint DEFAULT NULL, p_document jsonb DEFAULT NULL,
 p_event jsonb DEFAULT NULL, p_key text DEFAULT NULL, p_hash text DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE g kadrian_sync_control.gate; result jsonb; target_agent text; target_workspace text; target_project uuid;
BEGIN
 -- SHARE lock lasts until this operation transaction commits. STOP takes an
 -- exclusive row lock. After STOP commits no stale generation can commit.
 SELECT * INTO g FROM kadrian_sync_control.gate
 WHERE owner_id=p_owner AND agent_id=p_agent AND workspace_id=p_workspace FOR SHARE;
 IF NOT FOUND OR NOT g.enabled OR g.release_id IS DISTINCT FROM p_release
 OR g.device_id IS DISTINCT FROM p_device OR g.project_id IS DISTINCT FROM p_project
 OR (g.expires_at IS NOT NULL AND g.expires_at<=clock_timestamp())
 THEN RAISE EXCEPTION 'Sync unavailable'; END IF;
 IF p_action='begin' THEN RETURN jsonb_build_object('generation',g.generation); END IF;
 IF p_generation IS DISTINCT FROM g.generation THEN RAISE EXCEPTION 'Sync unavailable'; END IF;
 target_agent=p_scope->>'agent_id'; target_workspace=p_scope->>'workspace_id';
 target_project=(p_scope->>'project_id')::uuid;
 IF (p_scope->>'owner_id')::bigint IS DISTINCT FROM p_owner OR ((
   (target_agent=p_agent AND target_workspace=p_workspace AND target_project=p_project)
   OR (target_agent IN ('*',p_agent) AND target_workspace='owner' AND target_project IS NULL)
 ) IS NOT TRUE) THEN RAISE EXCEPTION 'Sync unavailable'; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.content_factory_projects WHERE id=p_project AND owner_id=p_owner)
 THEN RAISE EXCEPTION 'Sync unavailable'; END IF;
 IF EXISTS(SELECT 1 FROM public.agent_memory_workspaces WHERE owner_id=p_owner AND agent_id=target_agent
   AND workspace_id=target_workspace AND project_id IS DISTINCT FROM target_project)
 THEN RAISE EXCEPTION 'Sync unavailable'; END IF;
 IF p_action='metadata' THEN
   SELECT COALESCE(jsonb_agg(x),'[]'::jsonb) INTO result FROM
    (SELECT revision,hash FROM public.agent_memory_revisions WHERE owner_id=p_owner
     AND agent_id=target_agent AND workspace_id=target_workspace ORDER BY revision DESC LIMIT 1) x;
 ELSIF p_action='read' THEN
   SELECT COALESCE(jsonb_agg(x),'[]'::jsonb) INTO result FROM
    (SELECT revision,document,event,hash,idempotency_key,created_at FROM public.agent_memory_revisions
     WHERE owner_id=p_owner AND agent_id=target_agent AND workspace_id=target_workspace ORDER BY revision DESC LIMIT 1) x;
 ELSIF p_action='commit' THEN
   IF target_agent<>p_agent OR target_workspace<>p_workspace OR target_project IS DISTINCT FROM p_project
   THEN RAISE EXCEPTION 'Sync unavailable'; END IF;
   SELECT jsonb_agg(x) INTO result FROM public.agent_memory_commit(p_owner,p_agent,p_workspace,p_project,
      p_expected,p_document,p_event,p_key,p_hash) x;
 ELSE RAISE EXCEPTION 'Sync unavailable'; END IF;
 RETURN result;
END; $$;
REVOKE ALL ON FUNCTION public.kadrian_memory_exchange(text,text,text,bigint,text,text,uuid,bigint,jsonb,bigint,jsonb,jsonb,text,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.kadrian_memory_exchange(text,text,text,bigint,text,text,uuid,bigint,jsonb,bigint,jsonb,jsonb,text,text) TO service_role;
COMMIT;
-- Deliberately no inserted binding, enabling statement, cron, or existing RPC change.
-- Runtime receives NO control schema/table privileges. Only operator SQL can change it.
