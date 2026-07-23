-- OMN-14894 (tranche 1): tenant_id + row-level tenant isolation for
-- node_service_registry.
--
-- Unlike delegation_events (0022) and savings_estimates (080), this table
-- has no tenant_id column in the migration tree yet — OMN-14894 names it as
-- one of the three tables already carrying RLS on the manually-migrated
-- cloud database, so this migration brings the source-controlled tree to
-- parity: column + index + ENABLE/FORCE RLS + tenant_isolation policy +
-- SELECT grant to app_dashboard (OMN-14899's non-owner, NOSUPERUSER,
-- NOBYPASSRLS read role).
--
-- DEFAULT 'omninode' mirrors the interim single-tenant convention used by
-- 0022/080: existing rows and unstamped registration writes land under the
-- default tenant; a writer-supplied tenant_id overrides it.
--
-- SEAM DECISION: tenant_id TEXT, policy compares TEXT (no ::uuid cast) —
-- consistent with every landed tenant_id column on this projection surface.
-- See node_projection_delegation/0023 for the full rationale.
--
-- BLAST RADIUS: FORCE constrains the table owner. Compose-lane writers are
-- the postgres SUPERUSER (never subject to RLS), so unaffected. Any
-- non-superuser owner-writer must SET app.tenant_id before this applies to
-- its database.

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

ALTER TABLE node_service_registry
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'omninode';

CREATE INDEX IF NOT EXISTS idx_node_service_registry_tenant_id
    ON node_service_registry (tenant_id);

ALTER TABLE node_service_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE node_service_registry FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON node_service_registry;
CREATE POLICY tenant_isolation ON node_service_registry
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT ON node_service_registry TO app_dashboard;
