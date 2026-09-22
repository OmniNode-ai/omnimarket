-- =============================================================================
-- MIGRATION: deliver the omninode_runtime grants for prod_promotion_gate_decisions
-- =============================================================================
-- Ticket:  OMN-18999 (surface 2 of 4 under OMN-18946)
-- Owner:   omnimarket.nodes.node_projection_prod_promotion_gate
-- Version: 1.0.0
--
-- WHY THIS EXISTS
--   `0000` creates the relation; the deployment topology derives
--   `principals.omninode_runtime.grants[object_type: TABLE, ...
--   prod_promotion_gate_decisions]` from this node's own `db_io.db_tables`. A
--   DECLARED grant with no delivering migration is an outage waiting for the
--   relation to take traffic, and check_topology_grant_delivery.py refuses
--   it. A live ACL observed on one lane is not a delivered grant: nothing
--   replays it onto a fresh database, which is the whole point of a migration
--   lineage.
--
-- THE SEQUENCE HALF IS THE ONE THAT MATTERS
--   `projection_cursor` is BIGSERIAL, so every INSERT evaluates a nextval()
--   DEFAULT over a STANDALONE sequence whose own ACL PostgreSQL checks
--   separately. A table grant alone does not make the relation writable. That
--   is OMN-17379 exactly: pr_merged_events sat 24 days behind its topic at
--   consumer LAG 0 -- the consumer read fine and every write failed on the
--   sequence -- and the reason it shipped broken is that its migration
--   asserted only the table INSERT, which was TRUE throughout. Both halves
--   are granted here and BOTH are asserted.
--
-- WHY THE SEQUENCE IS NAMED STATICALLY
--   A `DO $$ ... EXECUTE format(...)` resolution is refused by OMN-15361's
--   SQL ownership gate -- a static reader cannot prove which relation a
--   dynamically composed statement will touch. The literal name is used
--   instead, and assertion 1 restores what the dynamic form gave for free:
--   the migration fails unless pg_get_serial_sequence resolves THIS column to
--   THAT sequence, so a restore, a rename or an out-of-band apply is a loud
--   failure rather than a silent half-grant.
--
-- WHY SELECT AND UPDATE ARE GRANTED WHEN db_io SAYS `write`
--   The writer's statement is an INSERT ... ON CONFLICT DO UPDATE and its
--   RETURNING clause reads the accepted row back. A lane where INSERT landed
--   and UPDATE did not would accept the first decision for a run and fail
--   every redelivery; one where SELECT did not land would fail the read-back
--   that proves the write. No DELETE: a gate decision is history and is never
--   retracted.
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
GRANT SELECT, INSERT, UPDATE ON omninode_internal.prod_promotion_gate_decisions TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 3. Sequence grant -- the half whose absence is invisible to a table-only
--    check. Named statically so the ownership gate can prove the target;
--    assertion 1 proves the name is the right one.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SEQUENCE omninode_internal.prod_promotion_gate_decisions_projection_cursor_seq TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 4. Assertions: fail the migration if any fact does not hold. Division by
--    zero when the fact is false -- the fail-loud shape this repository's
--    other grant migrations already use. Every granted privilege is asserted,
--    not only INSERT: asserting only INSERT is exactly what let OMN-17379
--    ship, because it was TRUE for the whole 24-day outage.
-- ---------------------------------------------------------------------------
SELECT 1 / count(*) AS prod_promotion_gate_cursor_sequence_identity_assertion
WHERE pg_get_serial_sequence(
          'omninode_internal.prod_promotion_gate_decisions', 'projection_cursor'
      ) = 'omninode_internal.prod_promotion_gate_decisions_projection_cursor_seq';

SELECT 1 / count(*) AS prod_promotion_gate_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'prod_promotion_gate_decisions'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS prod_promotion_gate_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'prod_promotion_gate_decisions'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS prod_promotion_gate_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'prod_promotion_gate_decisions'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';

SELECT 1 / count(*) AS prod_promotion_gate_cursor_sequence_usage_assertion
WHERE has_sequence_privilege(
          'omninode_runtime',
          'omninode_internal.prod_promotion_gate_decisions_projection_cursor_seq',
          'USAGE'
      );
