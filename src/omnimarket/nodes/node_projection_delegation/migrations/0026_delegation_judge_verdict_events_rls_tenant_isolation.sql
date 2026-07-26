-- OMN-14894 (tranche 2): row-level tenant isolation for
-- delegation_judge_verdict_events.
--
-- delegation_judge_verdict_events is written by the same node
-- (node_projection_delegation) and lives in the same migration directory as
-- the tranche-1-landed delegation_events/delegation_budget_state (0016 sits
-- alongside 0023). tenant_id was added and backfilled by migration 0025 in
-- this same tranche via a correlation_id join to delegation_events, with a
-- DEFAULT_TENANT ('omninode') fallback for unmatched rows.
--
-- Same shape as 0023, repeated here rather than factored out so each
-- migration file stays independently replayable:
--
--   ENABLE + FORCE ROW LEVEL SECURITY
--   POLICY tenant_isolation: tenant_id = current_setting('app.tenant_id', true)
--   GRANT SELECT to app_dashboard (OMN-14899, omnibase_infra forward
--   migration 094)
--
-- SEAM DECISION -- tenant_id stays TEXT, policy compares TEXT (no ::uuid):
--   same reasoning as 0023 -- every landed tenant_id column on this surface
--   is TEXT in slug form.
--
-- BLAST RADIUS -- FORCE constrains the table OWNER too:
--   identical caveat to 0023 -- writers connecting as the postgres
--   SUPERUSER on compose lanes bypass RLS regardless of FORCE.
--
-- JOIN-COMPLETENESS CAVEAT (see 0025): the tenant_id backfill/write join
-- against delegation_events was only verified against a 4-row stability-test
-- sample with a 50% miss rate. A miss defaults to 'omninode', so an
-- unresolved judge-verdict row is visible under the 'omninode' tenant
-- context, never silently dropped -- but this is not yet a certified join
-- rate. Re-verify once row volume grows; this policy does not depend on the
-- join being complete to be internally consistent (it isolates whatever
-- tenant_id value the row carries, default included).
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

GRANT USAGE ON SCHEMA public TO app_dashboard;

ALTER TABLE delegation_judge_verdict_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE delegation_judge_verdict_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON delegation_judge_verdict_events;
CREATE POLICY tenant_isolation ON delegation_judge_verdict_events
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT ON delegation_judge_verdict_events TO app_dashboard;
