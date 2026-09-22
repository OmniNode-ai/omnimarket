-- OMN-18903: omninode_runtime grants for omninode_internal.ci_attempt_outcome.
-- Target DB: omnidash_analytics (NODE_POSTGRES_DB)
-- Node: node_projection_ci_attempt_outcome
--
-- ============================================================================
-- WHY A GRANT FILE AT ALL
-- ============================================================================
--   The topology DECLARES these grants -- the TABLE list in
--   omnibase_infra/src/omnibase_infra/topology/instances/{local,onex-dev,
--   onex-prod}.yaml is GENERATED from the owning node's db_io.db_tables block
--   -- but declaring a grant is not issuing one. The two halves drifted apart
--   silently for years and the drift only ever surfaced as a live outage on
--   whichever relation took traffic next: OMN-17377 found projections sitting
--   at ZERO ROWS, and OMN-17379 reproduced the cause on the real wired path,
--   where every write failed with InsufficientPrivilege while the consumer
--   group reported Stable at LAG 0 and committed its offsets anyway.
--
--   That failure mode is exactly the one this node is most exposed to, for a
--   second and independent reason: a projection whose writer half is wired
--   wrongly ALSO reports zero rows with no error. Two silent-zero paths over
--   one relation is a good reason to assert the grants rather than declare
--   them.
--
--   The grant rides in the OWNING node's lineage, beside the file that
--   creates the relation, and deliberately NOT in a shared cross-node grant
--   file. A shared file is how a relation added to a node later silently
--   misses its grant; OMN-15701 is that failure, where one pin regeneration
--   reverted eight grants at once because they all lived in one place.
--
-- ============================================================================
-- WHY NO DELETE
-- ============================================================================
--   The platform's derived write set for a projection writer is
--   SELECT/INSERT/UPDATE, on the stated invariant that a projection writer
--   upserts and does not reshape the table. That invariant holds here without
--   qualification. This relation is append-and-correct: a row is one
--   (check, attempt) outcome that either happened or did not, so nothing is
--   ever deregistered and an absence is never meaningful. The runner-fleet
--   read model needed DELETE because a disappeared runner had to stop
--   reporting online; nothing analogous exists for an attempt that already
--   completed.
--
-- ============================================================================
-- WHY THE SEQUENCE GRANT IS A SEPARATE, NECESSARY HALF
-- ============================================================================
--   projection_cursor is BIGSERIAL. PostgreSQL rewrites that into a plain
--   nextval() DEFAULT over a STANDALONE sequence and checks that sequence's
--   OWN acl on every INSERT; GRANT INSERT ON TABLE does not reach it. So
--   omninode_runtime can hold a complete, correct table grant and still fail
--   every write with:
--
--     InsufficientPrivilege: permission denied for sequence
--     ci_attempt_outcome_projection_cursor_seq
--
--   (An IDENTITY column would not need this -- its sequence is owned by the
--   column and rides the table's INSERT privilege. This is BIGSERIAL, which
--   is precisely the distinction.) This is OMN-17447's class.
--
-- Idempotency: GRANT is idempotent; re-running is a no-op. Nothing here
-- touches RLS, ownership, or any role attribute.

-- ---------------------------------------------------------------------------
-- 1. Schema USAGE, mirroring the topology's SCHEMA grant. Re-asserted rather
--    than assumed: a migration must not depend on a sibling file having run.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 2. Table grants.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.ci_attempt_outcome
    TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 3. Sequence grant behind the BIGSERIAL projection_cursor.
--
--    The sequence is named STATICALLY, not resolved through
--    pg_get_serial_sequence inside a procedural block: omnibase_infra's
--    OMN-15361 SQL gate refuses a DO block carrying dynamic SQL, on the
--    correct ground that it cannot prove which relations such a block
--    touches. The literal spelling is safe HERE specifically because 0000 in
--    this same lineage creates the column as BIGSERIAL and nothing renames
--    it. The assertion below proves the grant landed on the sequence that
--    actually backs the column, so a spelling that ever stopped matching
--    fails the migration rather than granting nothing quietly.
-- ---------------------------------------------------------------------------
GRANT USAGE
    ON SEQUENCE omninode_internal.ci_attempt_outcome_projection_cursor_seq
    TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 4. Assertions: fail the migration if a grant did not take.
--
--    Every privilege is asserted, not just INSERT. Asserting only the table
--    INSERT is what let the broken state ship for pr_merged_events -- it was
--    TRUE for the whole 24-day outage while the sequence privilege, the one
--    actually missing, was never checked.
-- ---------------------------------------------------------------------------
SELECT 1 / count(*) AS ci_attempt_outcome_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'ci_attempt_outcome'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS ci_attempt_outcome_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'ci_attempt_outcome'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS ci_attempt_outcome_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'ci_attempt_outcome'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';

SELECT 1 / count(*) AS ci_attempt_outcome_cursor_sequence_usage_assertion
WHERE has_sequence_privilege(
          'omninode_runtime',
          pg_get_serial_sequence(
              'omninode_internal.ci_attempt_outcome', 'projection_cursor'
          ),
          'USAGE'
      );
