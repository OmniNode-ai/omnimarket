-- OMN-19978: row-level security and grants for the TENANT-domain usage tables.
-- Same shape as node_projection_cost_summary/0002 (RLS on tenant_id against the
-- app.tenant_id GUC) and node_projection_delegation_inference_response/0004
-- (writer role grants). Both tables carry tenant_id, so they are TENANT domain
-- in `public` (operator ruling 2026-09-24), never omninode_internal.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname = 'app_dashboard' AND NOT rolsuper AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION
      'app_dashboard role missing or RLS-bypassing - apply omnibase_infra forward migration '
      '094_create_app_dashboard_role.sql (OMN-14899) before this RLS migration.';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'tenant_projection_writer') THEN
    CREATE ROLE tenant_projection_writer WITH NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
END
$$;

ALTER TABLE public.usage_by_model_day_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.usage_by_model_day_calls FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.usage_by_model_day_calls;
CREATE POLICY tenant_isolation ON public.usage_by_model_day_calls
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE public.usage_by_model_day ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.usage_by_model_day FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.usage_by_model_day;
CREATE POLICY tenant_isolation ON public.usage_by_model_day
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT USAGE ON SCHEMA public TO app_dashboard;
GRANT SELECT ON public.usage_by_model_day TO app_dashboard;
GRANT SELECT ON public.usage_by_model_day_calls TO app_dashboard;

GRANT USAGE ON SCHEMA public TO tenant_projection_writer;
GRANT SELECT, INSERT, UPDATE
    ON public.usage_by_model_day_calls, public.usage_by_model_day
    TO tenant_projection_writer;
GRANT USAGE ON SEQUENCE public.usage_by_model_day_projection_cursor_seq
    TO tenant_projection_writer;
