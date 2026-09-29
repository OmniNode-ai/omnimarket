-- OMN-19978: row-level security and grants for the TENANT-domain usage tables.
-- Same shape as node_projection_cost_summary/0002 (RLS on tenant_id against the
-- app.tenant_id GUC) and node_projection_delegation_inference_response/0004
-- (writer role grants). Both tables carry tenant_id, so they are TENANT domain
-- in `public` (operator ruling 2026-09-24), never omninode_internal.

DO $$
DECLARE
  executing_role text := current_user;
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
    BEGIN
      CREATE ROLE tenant_projection_writer WITH NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
      RAISE NOTICE 'created role tenant_projection_writer as %', executing_role;
    EXCEPTION
      WHEN duplicate_object OR unique_violation THEN
        -- Roles are cluster-wide; two migration paths may race. Not an error.
        NULL;
      WHEN insufficient_privilege THEN
        RAISE EXCEPTION USING
          ERRCODE = 'insufficient_privilege',
          MESSAGE = format(
            'tenant_projection_writer does not exist on this cluster and the '
            'executing role %I cannot create it: CREATE ROLE requires the '
            'CREATEROLE attribute, which every migration identity is '
            'deliberately provisioned without.', executing_role),
          DETAIL =
            'PostgreSQL roles are cluster-scoped. On the managed (RDS) lane the '
            'migrate Job holds only role_omnibase_infra and role_omnidash, both '
            'NOCREATEROLE by contract, and the instance has no superuser role '
            'this Job can authenticate as (OMN-15343). Relocating this DDL to '
            'the node migration loop does not help -- role_omnidash lacks the '
            'same attribute. This migration refuses to record itself against a '
            'principal that is not there: topology/application_database.py binds '
            'tenant_projection -> tenant_projection_writer and OMN-16911 attests '
            'current_user on every projection connection, so a silent skip would '
            'resurface as total DLQ loss on the tenant projections instead of as '
            'this message.',
          HINT =
            'Provision the role once at the seam that holds the privilege, then '
            're-run this deploy -- this file becomes an idempotent no-op. From '
            'omninode_infra, with the instance master credential in the '
            'environment: scripts/provision-cluster-roles.sh --apply '
            '(dry run by default; --help for the credential variables). '
            'Ticket: OMN-17301, class OMN-15343.';
    END;
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
