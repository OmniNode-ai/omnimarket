-- OMN-15425 (P5): authorize the tenant_projection_writer principal on the
-- tenant-classified relations the platform topology declares for it.
--
-- ============================================================================
-- WHY THIS FILE EXISTS
-- ============================================================================
-- omnibase_infra forward migration 102 creates the ROLE (cluster-wide, NOLOGIN,
-- NOSUPERUSER, NOBYPASSRLS) and grants CONNECT on omnidash_analytics. It cannot
-- carry the schema/table half: a NEW flat migration whose `\connect` names a
-- database other than `omnibase_infra` is a hard reject under that repo's
-- tests/ci/test_flat_migration_no_foreign_connect_gate.py, because the k8s Job
-- applying the flat corpus gates its `psql -f` loop on
-- `directive_db == "$DB_NAME"` and can never deliver such a file (OMN-15819).
-- The node-owned loop is the sanctioned path that connects directly to
-- omnidash_analytics (NODE_POSTGRES_DB), so the AUTHORIZATION rides here.
--
-- The defect this closes, measured on the .201 dev lane 2026-08-29: with the
-- role absent, every tenant projection failed OMN-16911's connection-identity
-- attestation and this node's own handler DLQ'd 143/143 messages with
--
--   PermissionError: Projection binding 'tenant_projection' connected as
--     ('role_omnidash', 'omnidash_analytics'),
--     expected ('tenant_projection_writer', 'omnidash_analytics')
--
-- ============================================================================
-- WHY THE FULL TENANT SET, AND WHY IT LIVES UNDER THIS NODE
-- ============================================================================
-- OMN-15425 repoints the `tenant_projection` binding at its own DSN
-- (ONEX_TENANT_DB_URL) for EVERY tenant-domain projection at once, not just
-- this one — one binding, one login role. Granting only this node's table would
-- move the other sixteen from "wrong login role" to "permission denied on the
-- relation": a different error, the same zero rows. So the grant set here is
-- the complete declared set, applied in one place.
--
-- The list below is a verbatim transcription of
--   omnibase_infra/src/omnibase_infra/topology/instances/*.yaml
--   principals.tenant_projection_writer.grants[object_type: TABLE, schema: public]
-- which is itself GENERATED from node contract `db_io.db_tables` declarations by
-- omnibase_infra/scripts/generate_application_database_table_grants.py --write.
-- It is not a hand-picked set and must not be edited by hand: if a contract adds
-- a tenant-classified table, regenerate the topology and add the name here in
-- the same change. The privileges are exactly SELECT + INSERT + UPDATE and
-- deliberately NOT DELETE — a projection writer upserts, it does not reshape the
-- table (same invariant 096 states for role_omnidash and 099 for
-- omninode_runtime).
--
-- Homing a cross-node grant block under one node is a real ownership compromise
-- and is called out rather than hidden. It is homed here because this is the
-- node in the live outage, and because the alternative — sixteen near-identical
-- files, one per owning node — multiplies the "author copies the CREATE TABLE
-- and forgets the GRANT" defect class that 099's header already names.
-- OMN-15355 (generated ACL/default-privilege matrix, the systematic successor)
-- supersedes this file's intent when it lands; until then this is the delivery
-- seam.
--
-- ============================================================================
-- WHY NO `ALTER DEFAULT PRIVILEGES IN SCHEMA public`
-- ============================================================================
-- 099 uses that shortcut for schema `omninode_internal`, where every future
-- table belongs to one domain. `public` is NOT that: on this database it still
-- physically hosts ~40 OMNINODE_INTERNAL-domain families bridged there pending
-- their own cutovers (098's deferred-work list). A default-privileges grant in
-- `public` would hand tenant_projection_writer SELECT/INSERT/UPDATE on every
-- future internal-domain table as it appears — silently violating this
-- ticket's own acceptance criterion that "tenant projection writes cannot
-- access internal relations". The explicit, contract-derived list is the point,
-- not an omission.
--
-- ============================================================================
-- IDEMPOTENCY / FAIL-SAFETY
-- ============================================================================
--   * The role check fails LOUD and names the migration that provisions it.
--     A grant to a non-existent role would abort the whole node loop anyway;
--     this makes the reason legible instead of a bare 42704.
--   * Each table grant is guarded on `to_regclass` — a table whose own node
--     migration has not yet run is skipped, not an error. Re-running this file
--     after that table appears grants it.
--   * GRANT is idempotent in PostgreSQL. Re-running changes nothing.
--   * Schema `tenant` is guarded on existence: the topology declares USAGE on
--     it, but the physical schema does not exist on every lane yet (live
--     readback, .201 dev lane 2026-08-29: pg_namespace holds public,
--     omninode_internal, platform_catalog — no `tenant`). Asserting it here
--     would fail the lane for a schema the P2-P4 tranche owns.
--   * NOTHING here touches RLS, ownership, or any role attribute. The
--     tenant_isolation policies stay exactly as 0003 and its siblings left
--     them, and tenant_projection_writer being non-owner + NOBYPASSRLS is what
--     makes them apply to it.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'tenant_projection_writer') THEN
    CREATE ROLE tenant_projection_writer WITH NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
END
$$;

SELECT 'tenant_projection_writer'::regrole;

-- Schema USAGE. `public` is where the tenant-classified relations physically
-- live today; `tenant` is the declared destination of the P2-P4 tranche and is
-- granted opportunistically so this file needs no revision the day it appears.
GRANT USAGE ON SCHEMA public TO tenant_projection_writer;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = 'tenant') THEN
    GRANT USAGE ON SCHEMA tenant TO tenant_projection_writer;
  END IF;
END
$$;

-- Per-table SELECT, INSERT, UPDATE. PostgreSQL requires SELECT alongside
-- INSERT/UPDATE for the adapter's `INSERT ... ON CONFLICT DO UPDATE`, which is
-- why the write set is three privileges and not two — the same requirement
-- omnibase_infra's `_require_projection_binding_privileges` encodes.
DO $$
BEGIN
  IF to_regclass('public.agent_routing_decisions') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.agent_routing_decisions TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.capability_scores') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.capability_scores TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.context_roi_scores') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.context_roi_scores TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.delegation_budget_state') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.delegation_budget_state TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.delegation_events') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.delegation_events TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.delegation_judge_verdict_events') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.delegation_judge_verdict_events TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.delegation_routing_tenant_overlay') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.delegation_routing_tenant_overlay TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.delegation_shadow_comparisons') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.delegation_shadow_comparisons TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.dep_health_findings') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.dep_health_findings TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.hook_events') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.hook_events TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.instruction_eval_aggregate_snapshots') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.instruction_eval_aggregate_snapshots TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.llm_cost_aggregates') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.llm_cost_aggregates TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.pattern_learning_artifacts') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.pattern_learning_artifacts TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.projection_delegation_inference_response_text') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.projection_delegation_inference_response_text TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.savings_estimates') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.savings_estimates TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.skill_execution_snapshots') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.skill_execution_snapshots TO tenant_projection_writer;
  END IF;
  IF to_regclass('public.tenant_inference_credentials') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE ON public.tenant_inference_credentials TO tenant_projection_writer;
  END IF;
END
$$;
