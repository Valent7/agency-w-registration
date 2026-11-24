-- Additive; intended for a separately approved pilot migration.
BEGIN;
CREATE TABLE IF NOT EXISTS public.marketing_budgets (
    owner_id bigint NOT NULL,
    project_id uuid NOT NULL,
    agent_id text NOT NULL CHECK (agent_id IN ('neonia','neona','kadrian','ora','stagirite')),
    daily_cents bigint NOT NULL CHECK (daily_cents BETWEEN 0 AND 100000000),
    monthly_cents bigint NOT NULL CHECK (monthly_cents BETWEEN daily_cents AND 100000000),
    PRIMARY KEY (owner_id, project_id, agent_id),
    FOREIGN KEY (project_id, owner_id) REFERENCES public.content_factory_projects (id, owner_id)
);

CREATE TABLE IF NOT EXISTS public.marketing_spend (
    owner_id bigint NOT NULL,
    project_id uuid NOT NULL,
    agent_id text NOT NULL,
    operation_id uuid NOT NULL,
    budget_day date NOT NULL DEFAULT (now() AT TIME ZONE 'Europe/Berlin')::date,
    estimate_cents bigint NOT NULL CHECK (estimate_cents BETWEEN 1 AND 100000000),
    actual_cents bigint CHECK (actual_cents BETWEEN 0 AND 100000000),
    PRIMARY KEY (owner_id, project_id, agent_id, operation_id),
    FOREIGN KEY (owner_id, project_id, agent_id)
        REFERENCES public.marketing_budgets (owner_id, project_id, agent_id)
);
CREATE INDEX IF NOT EXISTS marketing_spend_scope_day
ON public.marketing_spend (owner_id, project_id, agent_id, budget_day);

ALTER TABLE public.marketing_budgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.marketing_spend ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.marketing_budgets, public.marketing_spend FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON public.marketing_budgets, public.marketing_spend TO service_role;

CREATE OR REPLACE FUNCTION public.marketing_reserve_spend(
    p_owner bigint, p_project uuid, p_agent text, p_operation uuid, p_estimate bigint
) RETURNS text
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, pg_temp AS $$
DECLARE
    budget public.marketing_budgets;
    billing_day date := (now() AT TIME ZONE 'Europe/Berlin')::date;
    month_start date := date_trunc('month', billing_day::timestamp)::date;
    day_total numeric;
    month_total numeric;
    previous_estimate bigint;
BEGIN
    IF p_estimate IS NULL OR p_estimate <= 0 OR p_operation IS NULL THEN
        RAISE EXCEPTION 'Invalid spending reservation';
    END IF;
    SELECT * INTO budget FROM public.marketing_budgets
        WHERE owner_id=p_owner AND project_id=p_project AND agent_id=p_agent FOR UPDATE;
    IF NOT FOUND THEN RETURN 'no_budget'; END IF;
    SELECT estimate_cents INTO previous_estimate FROM public.marketing_spend
        WHERE owner_id=p_owner AND project_id=p_project AND agent_id=p_agent
          AND operation_id=p_operation;
    IF FOUND THEN
        IF previous_estimate <> p_estimate THEN RAISE EXCEPTION 'Reservation mismatch'; END IF;
        RETURN 'already_reserved';
    END IF;
    SELECT coalesce(sum(coalesce(actual_cents,estimate_cents)) FILTER (WHERE budget_day=billing_day),0),
           coalesce(sum(coalesce(actual_cents,estimate_cents)),0)
      INTO day_total,month_total FROM public.marketing_spend
     WHERE owner_id=p_owner AND project_id=p_project AND agent_id=p_agent
       AND budget_day >= month_start AND budget_day < month_start + interval '1 month';
    IF day_total+p_estimate > budget.daily_cents OR month_total+p_estimate > budget.monthly_cents THEN
        RETURN 'limit_reached';
    END IF;
    INSERT INTO public.marketing_spend(owner_id, project_id, agent_id, operation_id, budget_day, estimate_cents)
    VALUES(p_owner,p_project,p_agent,p_operation,billing_day,p_estimate);
    RETURN 'reserved';
END;
$$;

CREATE OR REPLACE FUNCTION public.marketing_record_spend(
    p_owner bigint, p_project uuid, p_agent text, p_operation uuid, p_actual bigint
) RETURNS boolean
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, pg_temp AS $$
BEGIN
    IF p_actual IS NULL OR p_actual < 0 THEN RAISE EXCEPTION 'Invalid actual cost'; END IF;
    PERFORM 1 FROM public.marketing_budgets
        WHERE owner_id=p_owner AND project_id=p_project AND agent_id=p_agent FOR UPDATE;
    UPDATE public.marketing_spend SET actual_cents=p_actual
        WHERE owner_id=p_owner AND project_id=p_project AND agent_id=p_agent
          AND operation_id=p_operation AND (actual_cents IS NULL OR actual_cents=p_actual);
    RETURN FOUND;
END;
$$;
REVOKE ALL ON FUNCTION public.marketing_reserve_spend(bigint,uuid,text,uuid,bigint)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.marketing_record_spend(bigint,uuid,text,uuid,bigint)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.marketing_reserve_spend(bigint,uuid,text,uuid,bigint) TO service_role;
GRANT EXECUTE ON FUNCTION public.marketing_record_spend(bigint,uuid,text,uuid,bigint) TO service_role;
COMMIT;
