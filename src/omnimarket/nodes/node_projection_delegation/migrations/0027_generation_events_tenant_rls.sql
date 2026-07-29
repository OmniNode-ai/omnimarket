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
--
-- OMN-15351 -- role_omnidash is ENVIRONMENT-provisioned, not migration-provisioned:
--   role_omnidash exists on cloud RDS (provisioned out-of-band) and on a compose
--   cluster only when ROLE_OMNIDASH_PASSWORD was configured at FIRST-STARTUP init
--   (omnibase_infra docker/migrations/forward/000_create_multiple_databases.sh).
--   NO forward migration anywhere creates it.  The .201 dev lane cluster carries
--   pg_roles = {app_dashboard, postgres, role_omniweb} -- no role_omnidash -- so
--   the original fail-closed RAISE EXCEPTION on its absence made EVERY dev-lane
--   deploy fatal at this file (OMN-15348 AC4 redeploy, workflow wf_55998f90).
--
--   The guard is therefore a WARNING that ENUMERATES the two grants it skips, and
--   those two grants execute only when the role exists.  Where the role exists the
--   applied grants are identical to the pre-OMN-15351 behaviour (same statements,
--   same order).  Where it does not, the database ends up with RLS enabled and NO
--   role_omnidash writer grant -- the honest state of a lane without that role,
--   logged by name, not silently skipped.  A lane in that state is not exercising
--   the RDS grant path and cannot prove it.
--
--   Creating role_omnidash locally is deliberately NOT done here: who owns role
--   creation (environment vs migration) is an architecture decision tracked
--   separately (OMN-15351 direction (b), needs OMN-14894 context).
--
-- app_dashboard stays FATAL on purpose: omnibase_infra forward migration 094
-- (OMN-14899) DOES create it in-repo, so its absence is a genuine migration
-- ordering bug rather than an environment difference.  That is the posture every
-- sibling RLS migration takes (node_projection_delegation 0023/0026,
-- node_projection_registration 0002, node_projection_savings 081).

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'role_omnidash') THEN
    RAISE WARNING
      'role_omnidash role missing — SKIPPING 2 grants on this database: '
      '(1) GRANT USAGE ON SCHEMA public TO role_omnidash; '
      '(2) GRANT SELECT, INSERT, UPDATE ON generation_events TO role_omnidash. '
      'generation_events writer access is NOT granted here. role_omnidash is '
      'environment-provisioned (RDS out-of-band, or ROLE_OMNIDASH_PASSWORD at '
      'cluster init) and is never created by a migration — see OMN-15351.';
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

-- role_omnidash grants, conditional on the environment-provisioned role existing
-- (OMN-15351 note above). The statement text and order are unchanged from the
-- pre-OMN-15351 top-level statements; only the existence guard is new.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'role_omnidash') THEN
    EXECUTE 'GRANT USAGE ON SCHEMA public TO role_omnidash';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE ON generation_events TO role_omnidash';
  END IF;
END;
$$;

GRANT USAGE ON SCHEMA public TO app_dashboard;
GRANT SELECT ON generation_events TO app_dashboard;
