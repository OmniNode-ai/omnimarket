-- OMN-14974: tenant-scoped writer/read access for generation_events.
--
-- The cloud migration runner creates node-owned tables as the bounded RDS
-- migration principal, while the runtime and dashboard connect as the
-- non-owner role_omnidash role.  generation_events was created by migration
-- 0008 without a tenant column, RLS policy, or grants, so the real generation
-- terminal reached the projection handler and failed with 42501 before a row
-- could become dashboard-visible.
--
-- This migration deliberately uses ENABLE without FORCE.  The live writer is
-- already a non-owner/NOSUPERUSER/NOBYPASSRLS role and is therefore subject to
-- RLS.  FORCE decisions for the migration owner remain fenced under the
-- OMN-15088 rollout; this migration must not silently cross that boundary.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'role_omnidash') THEN
    RAISE EXCEPTION
      'role_omnidash role missing — generation_events writer access cannot be granted';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_dashboard') THEN
    RAISE EXCEPTION
      'app_dashboard role missing — apply forward migration 094 before generation_events RLS';
  END IF;
END;
$$;

ALTER TABLE generation_events
  ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'omninode';

CREATE INDEX IF NOT EXISTS idx_generation_events_tenant_id
  ON generation_events (tenant_id);

ALTER TABLE generation_events ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON generation_events;
CREATE POLICY tenant_isolation ON generation_events
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT USAGE ON SCHEMA public TO role_omnidash;
GRANT SELECT, INSERT, UPDATE ON generation_events TO role_omnidash;

GRANT USAGE ON SCHEMA public TO app_dashboard;
GRANT SELECT ON generation_events TO app_dashboard;
