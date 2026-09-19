-- =============================================================================
-- MIGRATION: deliver the omninode_runtime grants for runtime_error_fingerprints
-- =============================================================================
-- Ticket:  OMN-18770 (C3 of epic OMN-18767 — lab observability tab)
-- Owner:   omnimarket.nodes.node_projection_runtime_error_fingerprints
-- Version: 1.0.0
--
-- WHY THIS EXISTS
--   `0000` creates the relation; the deployment topology declares
--   `principals.omninode_runtime.grants[object_type: TABLE, ...
--   runtime_error_fingerprints]`, derived from this node's own
--   `db_io.db_tables`. A DECLARED grant with no delivering migration is an
--   outage waiting for the relation to take traffic (OMN-16993, OMN-17374),
--   and `check_topology_grant_delivery.py` refuses it.
--
--   This file was not in the first cut of this change, and the PR body
--   asserted it was unnecessary because `omninode_internal`'s DEFAULT
--   PRIVILEGES already delivered the write set on the lab. That reasoning was
--   wrong twice. A live ACL observed on one lane is not a delivered grant --
--   nothing replays it onto a fresh database, which is the whole point of a
--   migration lineage. And the stated precedent was misread: the
--   `node_projection_consumer_flow` lineage ships BOTH halves, in `0003`
--   (table) and `0004` (sequence). This file is that pair, in one file,
--   for one relation.
--
-- THE SEQUENCE HALF IS THE ONE THAT MATTERS
--   `projection_cursor` is BIGSERIAL, so every INSERT evaluates a nextval()
--   DEFAULT over a STANDALONE sequence whose own ACL PostgreSQL checks
--   separately. A table grant alone does not make the relation writable. That
--   is OMN-17379 exactly: `pr_merged_events` sat 24 days behind its topic at
--   consumer LAG 0 -- the consumer was reading fine and every write was
--   failing on the sequence -- and the reason it shipped broken is that the
--   migration asserted only the table INSERT, which was TRUE throughout.
--   Both halves are granted here and BOTH are asserted.
--
-- WHY THE SEQUENCE IS NAMED STATICALLY
--   The first cut resolved it through a `DO $$ ... EXECUTE format(...)` block,
--   copying node_pr_merged_projection/0002. OMN-15361's SQL ownership gate
--   refuses that here -- "procedural block contains dynamic SQL whose relation
--   targets cannot be proven statically" -- and it is right to: a gate that
--   cannot see the target cannot tell this file apart from one that grants on
--   something else entirely. The literal name is used instead, exactly as
--   node_projection_consumer_flow/0004 does, and assertion 1 below restores
--   what the dynamic form gave for free: it fails the migration unless
--   pg_get_serial_sequence resolves THIS column to THAT sequence, so a
--   restore, a rename or an out-of-band apply is a loud failure rather than a
--   silent half-grant.
--
-- IDEMPOTENCY
--   GRANT is idempotent; re-running is a no-op.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Schema USAGE, mirroring topology. Idempotent, and re-asserted here
--    because a migration must not assume a sibling file ran.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 2. Table grant (topology-derived).
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON omninode_internal.runtime_error_fingerprints TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 3. Sequence grant -- the half whose absence is invisible to a table-only
--    check. Named statically so the ownership gate can prove the target;
--    assertion 1 proves the name is the right one.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SEQUENCE omninode_internal.runtime_error_fingerprints_projection_cursor_seq TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 4. Assertions: fail the migration if any fact does not hold. Division by
--    zero when the fact is false -- the fail-loud shape this repo's other
--    grant migrations already use.
--
--    1 is the identity check that replaces the dynamic resolution. 2-4 are the
--    table half; SELECT and UPDATE are asserted alongside INSERT because the
--    writer's upsert is `ON CONFLICT DO UPDATE`, and a lane where INSERT
--    landed and SELECT did not would accept writes and fail every read-back.
--    5 is the one this file exists for: asserting only the table INSERT is
--    exactly what let OMN-17379 ship, because it was TRUE for the whole
--    24-day outage.
-- ---------------------------------------------------------------------------
SELECT 1 / count(*) AS runtime_error_fingerprints_cursor_sequence_identity_assertion
WHERE pg_get_serial_sequence(
          'omninode_internal.runtime_error_fingerprints', 'projection_cursor'
      ) = 'omninode_internal.runtime_error_fingerprints_projection_cursor_seq';

SELECT 1 / count(*) AS runtime_error_fingerprints_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'runtime_error_fingerprints'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS runtime_error_fingerprints_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'runtime_error_fingerprints'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS runtime_error_fingerprints_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'runtime_error_fingerprints'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';

SELECT 1 / count(*) AS runtime_error_fingerprints_cursor_sequence_usage_assertion
WHERE has_sequence_privilege(
          'omninode_runtime',
          'omninode_internal.runtime_error_fingerprints_projection_cursor_seq',
          'USAGE'
      );
