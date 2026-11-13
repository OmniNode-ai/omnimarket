-- OMN-21933: the RLS-scoped dashboard reader needs SELECT on skill_executions.
-- 0002 enabled the tenant_isolation policy and is already applied, so it stays
-- byte-identical to the omnibase_infra vendored copy; the reader grant is its
-- own step here (OMN-14894: the policy is unreachable for app_dashboard without
-- SELECT on the relation; RLS still filters every row the role sees).
DO $require_app_dashboard$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'app_dashboard' AND NOT rolsuper AND NOT rolbypassrls
    ) THEN
        RAISE EXCEPTION
            'app_dashboard role missing or RLS-bypassing; apply omnibase_infra migration 094_create_app_dashboard_role.sql before this migration';
    END IF;
END
$require_app_dashboard$;

GRANT USAGE ON SCHEMA public TO app_dashboard;
GRANT SELECT ON public.skill_executions TO app_dashboard;

DO $assert_app_dashboard_privilege$
BEGIN
    IF NOT has_table_privilege('app_dashboard', 'public.skill_executions', 'SELECT') THEN
        RAISE EXCEPTION 'missing required app_dashboard privilege SELECT on public.skill_executions';
    END IF;
END
$assert_app_dashboard_privilege$;
