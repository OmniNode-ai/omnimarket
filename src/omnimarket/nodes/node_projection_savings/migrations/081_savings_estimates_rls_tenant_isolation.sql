-- OMN-14894 (tranche 1): row-level tenant isolation for savings_estimates.
--
-- tenant_id TEXT already exists (080, stamped by
-- HandlerProjectionSavings.project_delegate_skill_savings with DEFAULT
-- 'omninode'). This migration enforces it:
--
--   ENABLE + FORCE ROW LEVEL SECURITY
--   POLICY tenant_isolation: tenant_id = current_setting('app.tenant_id', true)
--   GRANT SELECT to app_dashboard (non-owner, NOSUPERUSER, NOBYPASSRLS —
--   created by omnibase_infra forward migration 094, OMN-14899)
--
-- SEAM DECISION: TEXT comparison, no ::uuid cast — landed tenant_id columns
-- on this surface are TEXT slug-form with existing 'omninode' rows; a uuid
-- cast would raise on every current row/write. See
-- node_projection_delegation/0023 for the full rationale.
--
-- BLAST RADIUS: FORCE constrains the table owner. Compose-lane writers are
-- the postgres SUPERUSER (never subject to RLS), so unaffected. Any
-- non-superuser owner-writer must SET app.tenant_id before this applies to
-- its database — preflight required for operator-gated cloud applies.
--
-- VIEW CAVEAT: the savings projection views (076/077/078) are owned by the
-- migration role; RLS is evaluated against the VIEW OWNER, so reads through
-- those views bypass tenant isolation. app_dashboard deliberately gets NO
-- grant on the views — base-table reads only — until the views are
-- recreated with security_invoker = true (follow-on tranche work).

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_dashboard') THEN
    RAISE EXCEPTION
      'app_dashboard role missing — apply omnibase_infra forward migration '
      '094_create_app_dashboard_role.sql (OMN-14899) before this RLS '
      'migration.';
  END IF;
END;
$$;

-- Schema resolution for the read role (role-only migration 094 carries no
-- grants by design; USAGE is granted here, alongside the policies).
GRANT USAGE ON SCHEMA public TO app_dashboard;

ALTER TABLE savings_estimates ENABLE ROW LEVEL SECURITY;
ALTER TABLE savings_estimates FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON savings_estimates;
CREATE POLICY tenant_isolation ON savings_estimates
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT ON savings_estimates TO app_dashboard;
