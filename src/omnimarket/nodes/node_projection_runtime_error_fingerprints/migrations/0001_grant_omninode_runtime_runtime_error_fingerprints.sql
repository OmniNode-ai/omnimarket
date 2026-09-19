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
-- 3. Sequence grant, resolved through `pg_get_serial_sequence` rather than by
--    spelling the sequence name, so a table whose sequence was created under a
--    different name (a restore, a rename, an out-of-band apply) still
--    converges. A NULL return means the column is not sequence-backed at all,
--    which would contradict `0000`'s BIGSERIAL declaration -- fail loud rather
--    than no-op into another silent half-grant.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_seq TEXT;
BEGIN
    v_seq := pg_get_serial_sequence(
        'omninode_internal.runtime_error_fingerprints', 'projection_cursor'
    );
    IF v_seq IS NULL THEN
        RAISE EXCEPTION
            'OMN-17379: omninode_internal.runtime_error_fingerprints.projection_cursor is not backed by a sequence, but 0000 declares it BIGSERIAL. Refusing to grant a privilege on an object that does not exist -- reconcile the column shape first.';
    END IF;
    EXECUTE format('GRANT USAGE ON SEQUENCE %s TO omninode_runtime', v_seq);
END$$;

-- ---------------------------------------------------------------------------
-- 4. Assertions: fail the migration if a half did not take. Division by zero
--    when the grant is absent -- the fail-loud shape this repo's other grant
--    migrations already use.
--
--    SELECT is asserted alongside INSERT because the writer's upsert is
--    `ON CONFLICT DO UPDATE ... RETURNING`: a lane where INSERT landed and
--    SELECT did not would accept writes and fail every read-back, which is the
--    harder of the two to diagnose. The SEQUENCE assertion is the OMN-17379
--    one -- the half whose absence is invisible to a table-only check.
-- ---------------------------------------------------------------------------
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
          pg_get_serial_sequence(
              'omninode_internal.runtime_error_fingerprints', 'projection_cursor'
          ),
          'USAGE'
      );
