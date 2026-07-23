-- OMN-14894 (tranche 1): row-level tenant isolation for delegation_events
-- and delegation_budget_state.
--
-- Both tables already carry tenant_id TEXT (0022 for delegation_events,
-- 0019 for delegation_budget_state) stamped by the projection writers.
-- This migration turns that column into an enforced boundary:
--
--   ENABLE + FORCE ROW LEVEL SECURITY
--   POLICY tenant_isolation: tenant_id = current_setting('app.tenant_id', true)
--   GRANT SELECT to app_dashboard (the non-owner, NOSUPERUSER, NOBYPASSRLS
--   runtime read role created by omnibase_infra forward migration 094,
--   OMN-14899)
--
-- SEAM DECISION — tenant_id stays TEXT, policy compares TEXT (no ::uuid):
--   OMN-14894's description quotes a `::uuid` cast, but every landed
--   tenant_id column on this surface is TEXT in slug form with rows already
--   holding 'omninode' (0019/0022 here, savings 080). A ::uuid cast would
--   raise on every existing row and on every current writer stamp. The
--   policy therefore compares TEXT against the app.tenant_id GUC. If tenant
--   keys later migrate to UUIDs, that is a coordinated column+writer+policy
--   change, not a policy-only edit.
--
-- BLAST RADIUS — FORCE constrains the table OWNER too:
--   On the compose lanes this tree deploys to, projection writers connect
--   as the postgres SUPERUSER, and superusers are never subject to RLS
--   (FORCE or not), so writers are unaffected there. Applying this to any
--   database whose writer is a NON-superuser owner requires that writer to
--   SET app.tenant_id (or the WITH CHECK clause rejects its INSERTs) —
--   preflight that before any operator-gated cloud apply.
--
-- Fail-closed: current_setting('app.tenant_id', true) is NULL when the GUC
-- is unset, the predicate is NULL, and zero rows are visible. No default-
-- tenant fallback exists in the policy by design.
--
-- Idempotent: guarded role check, ENABLE/FORCE are idempotent, policy is
-- DROP + CREATE, GRANT is idempotent.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_dashboard') THEN
    RAISE EXCEPTION
      'app_dashboard role missing — apply omnibase_infra forward migration '
      '094_create_app_dashboard_role.sql (OMN-14899) before this RLS '
      'migration. RLS grants without the constrained read role are the '
      'exact bypass this work exists to prevent.';
  END IF;
END;
$$;

-- Schema resolution for the read role (role-only migration 094 carries no
-- grants by design; USAGE is granted here, alongside the policies).
GRANT USAGE ON SCHEMA public TO app_dashboard;

-- ---------------------------------------------------------------------------
-- delegation_events
-- ---------------------------------------------------------------------------
ALTER TABLE delegation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE delegation_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON delegation_events;
CREATE POLICY tenant_isolation ON delegation_events
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT ON delegation_events TO app_dashboard;

-- ---------------------------------------------------------------------------
-- delegation_budget_state
-- ---------------------------------------------------------------------------
ALTER TABLE delegation_budget_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE delegation_budget_state FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON delegation_budget_state;
CREATE POLICY tenant_isolation ON delegation_budget_state
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT ON delegation_budget_state TO app_dashboard;

-- NOTE: no grants on the delegation projection VIEWS (0010). Postgres
-- evaluates RLS against the VIEW OWNER (postgres here), so reading through
-- an owner's view would silently bypass tenant isolation. app_dashboard
-- reads base tables only until the views are recreated with
-- security_invoker = true (follow-on tranche work).
