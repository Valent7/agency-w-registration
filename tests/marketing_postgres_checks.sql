-- Synthetic database only. Execute after installing marketing_interview_setup.sql twice.
INSERT INTO public.content_factory_projects(id,owner_id)
VALUES ('00000000-0000-0000-0000-000000000001',101),
       ('00000000-0000-0000-0000-000000000002',102);

SET ROLE service_role;
INSERT INTO public.marketing_interviews(owner_id,project_id,revision,encrypted_state)
VALUES (101,'00000000-0000-0000-0000-000000000001',1,'synthetic');

DO $$
BEGIN
    BEGIN
        INSERT INTO public.marketing_interviews(owner_id,project_id,revision,encrypted_state)
        VALUES (102,'00000000-0000-0000-0000-000000000001',1,'wrong owner');
        RAISE EXCEPTION 'Owner/project mismatch accepted';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;
    UPDATE public.marketing_interviews SET revision=2 WHERE owner_id=101 AND revision=1;
    IF NOT FOUND THEN RAISE EXCEPTION 'Expected current revision'; END IF;
    UPDATE public.marketing_interviews SET revision=2 WHERE owner_id=101 AND revision=1;
    IF FOUND THEN RAISE EXCEPTION 'Stale revision accepted'; END IF;
END;
$$;

INSERT INTO public.marketing_passport_versions(owner_id,project_id,version,encrypted_passport)
VALUES (101,'00000000-0000-0000-0000-000000000001',1,'synthetic');
INSERT INTO public.marketing_passport_versions(owner_id,project_id,version,restored_from,encrypted_passport)
VALUES (101,'00000000-0000-0000-0000-000000000001',2,1,'synthetic');
DO $$
BEGIN
    BEGIN
        UPDATE public.marketing_passport_versions SET encrypted_passport='changed';
        RAISE EXCEPTION 'Version mutation accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        INSERT INTO public.marketing_passport_versions
            (owner_id,project_id,version,restored_from,encrypted_passport)
        VALUES (102,'00000000-0000-0000-0000-000000000002',2,1,'foreign version');
        RAISE EXCEPTION 'Cross-project restore accepted';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;
END;
$$;

RESET ROLE;
SET ROLE authenticated;
DO $$
BEGIN
    BEGIN
        PERFORM * FROM public.marketing_interviews;
        RAISE EXCEPTION 'Client read accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        PERFORM * FROM public.marketing_passport_versions;
        RAISE EXCEPTION 'Client read accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END;
$$;
RESET ROLE;

SET ROLE service_role;
INSERT INTO public.marketing_budgets(owner_id,project_id,agent_id,daily_cents,monthly_cents)
VALUES (101,'00000000-0000-0000-0000-000000000001','neonia',10,30);
DO $$
DECLARE
    project uuid := '00000000-0000-0000-0000-000000000001';
    operation_one uuid := '10000000-0000-0000-0000-000000000001';
    operation_two uuid := '10000000-0000-0000-0000-000000000002';
BEGIN
    IF public.marketing_reserve_spend(102,project,'neonia',operation_one,7)<>'no_budget' THEN
        RAISE EXCEPTION 'Cross-owner spending permitted';
    END IF;
    IF public.marketing_reserve_spend(101,project,'neonia',operation_one,7)<>'reserved' OR
       public.marketing_reserve_spend(101,project,'neonia',operation_one,7)<>'already_reserved' THEN
        RAISE EXCEPTION 'Unexpected reservation status';
    END IF;
    IF public.marketing_reserve_spend(101,project,'neonia',operation_two,4)<>'limit_reached' THEN
        RAISE EXCEPTION 'Daily limit bypassed';
    END IF;
    IF NOT public.marketing_record_spend(101,project,'neonia',operation_one,5) THEN
        RAISE EXCEPTION 'Actual spend not recorded';
    END IF;
    IF public.marketing_reserve_spend(101,project,'neonia',operation_two,4)<>'reserved' THEN
        RAISE EXCEPTION 'Unexpected rejection after reconciliation';
    END IF;
END;
$$;
RESET ROLE;
SET ROLE anon;
DO $$
BEGIN
    BEGIN
        PERFORM public.marketing_reserve_spend(101,'00000000-0000-0000-0000-000000000001',
            'neonia','10000000-0000-0000-0000-000000000003',1);
        RAISE EXCEPTION 'Anonymous reservation granted';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END;
$$;
RESET ROLE;
