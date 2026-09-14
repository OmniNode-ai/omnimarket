-- OMN-14894: row-level tenant isolation for delegation_budget_state.
--
-- WHY THIS FILE EXISTS AT ALL, when 0023 already wrote this exact posture.
--   0023 (`0023_delegation_rls_tenant_isolation.sql`) put ENABLE + FORCE ROW
--   LEVEL SECURITY and a `tenant_isolation` policy on BOTH delegation_events
--   and delegation_budget_state in one file. delegation_events has since been
--   converted to a `uuid` tenant_id (0031 -> 0036 -> the operative 0037), and
--   0023's `CREATE POLICY ... USING (tenant_id = current_setting(...))`
--   compares TEXT to TEXT by construction, so 0023 now ABORTS against that
--   column. It is fenced as a SUPERSEDED id in
--   omnibase_infra/docker/migrations/forward/fenced-node-migrations.yaml and
--   stays fenced: releasing it would leave delegation_events FORCE-RLS with
--   zero policies.
--
--   delegation_events recovered its posture from 0037. delegation_budget_state
--   did NOT: nothing else in this corpus ever enabled RLS on it, so it is a
--   relation the OMN-15354 classification manifest classifies TENANT
--   (omnimarket/docs/evidence/OMN-15423-relation-inventory.json, domain
--   TENANT, classification_status classified, source
--   `contract.yaml -> db_io.db_tables schema` -> node_projection_delegation
--   contract.yaml `schema: tenant`) that carries no tenant boundary on any
--   lane. Measured 2026-09-14: relrowsecurity=f, relforcerowsecurity=f, 0
--   policies on BOTH the .201 compose dev lane and the onex-dev RDS on the
--   dev-system cluster. This file is 0023's second half, re-landed alone so
--   that the superseded first half stays buried.
--
-- WHAT IT DOES
--   ENABLE + FORCE ROW LEVEL SECURITY
--   POLICY tenant_isolation: tenant_id = current_setting('app.tenant_id', true)
--   GRANT SELECT to app_dashboard (non-owner, NOSUPERUSER, NOBYPASSRLS --
--   created by omnibase_infra forward migration 094, OMN-14899)
--
-- FAIL-CLOSED BY CONSTRUCTION: with `app.tenant_id` unset, the three-argument
-- `current_setting(..., true)` returns NULL, `tenant_id = NULL` evaluates to
-- NULL, and both USING and WITH CHECK therefore deny. An unset tenant context
-- reads zero rows and writes nothing; it never falls through to "all rows".
--
-- SEAM DECISION: TEXT comparison, no ::uuid cast. delegation_budget_state's
-- tenant_id is TEXT (0019) and 0030 rekeyed its unattributed rows onto the
-- house tenant slug. This matches savings_estimates (081) and every other
-- landed tenant_id column on this surface. Converting this column to uuid is
-- OMN-15356's coordinated column+writer+policy change, not a policy-only edit,
-- and it is deliberately NOT attempted here.
--
-- WHY THE 0026 HAZARD DOES NOT APPLY HERE, stated rather than assumed.
--   Releasing 0026 (the judge-verdict sibling) was measured on the .201 dev
--   lane to refuse every write the async writer issued, because that writer
--   called the adapter with no `tenant=` argument and the adapter fell back to
--   the table-less house-slug form, so the GUC and the column disagreed.
--   delegation_budget_state has no such split: BOTH of its write paths derive
--   the GUC from the row they are writing --
--     * sync: handler_budget_state.materialize_budget_state -> db.upsert ->
--       PostgresSyncProjectionAdapter.upsert, which computes
--       `resolve_write_tenant(row.get("tenant_id"), table=table)`;
--     * async: handler_delegation._materialize_budget_state_async ->
--       _dynamic_upsert, which resolves the write tenant from THIS row,
--       byte-for-byte mirroring the sync adapter (the OMN-15919 seam).
--   The GUC and the column therefore agree by construction on both paths.
--
-- BLAST RADIUS, measured 2026-09-14 rather than argued: delegation_budget_state
-- holds ZERO rows on the .201 compose dev lane (read as the lane superuser,
-- RLS off, so a true zero) and ZERO rows on the same onex-dev RDS (read as
-- role_omnidash with RLS off on this relation; positive control
-- tenant_registry_mirror = 112 rows on the same connection). No existing row
-- can be locked out because there are none. On onex-dev the table is owned by
-- role_omninode_owner and tenant_projection_writer already holds
-- INSERT/SELECT/UPDATE; app_dashboard holds nothing, which is the grant this
-- file adds. FORCE constrains the owner too -- on the compose lanes the
-- writers connect as the postgres SUPERUSER and are never subject to RLS.
--
-- VIEW CAVEAT, unchanged from 081/0023: the delegation projection views are
-- owned by the migration role and Postgres evaluates RLS against the VIEW
-- OWNER, so app_dashboard deliberately gets NO grant on any view here.
-- Base-table reads only until the views carry security_invoker = true.

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
-- grants by design; USAGE is granted here, alongside the policy).
GRANT USAGE ON SCHEMA public TO app_dashboard;

ALTER TABLE delegation_budget_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE delegation_budget_state FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON delegation_budget_state;
CREATE POLICY tenant_isolation ON delegation_budget_state
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT ON delegation_budget_state TO app_dashboard;
