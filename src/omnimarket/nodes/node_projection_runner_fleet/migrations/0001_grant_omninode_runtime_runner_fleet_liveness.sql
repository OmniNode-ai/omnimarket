-- OMN-18768: omninode_runtime grants for omninode_internal.runner_fleet_liveness.
-- Target DB: omnidash_analytics (NODE_POSTGRES_DB)
-- Node: node_projection_runner_fleet
--
-- ============================================================================
-- WHY A GRANT FILE AT ALL
-- ============================================================================
--   The topology DECLARES these grants -- the TABLE list in
--   omnibase_infra/src/omnibase_infra/topology/instances/{local,onex-dev,
--   onex-prod}.yaml is GENERATED from this node's own db_io.db_tables block --
--   but declaring a grant is not issuing one. The two halves drifted apart
--   silently for years and the drift was only ever found as a live outage on
--   whichever relation took traffic next: OMN-17377's survey found a row of
--   projections sitting at ZERO ROWS, and OMN-17379 reproduced the cause on
--   the real wired path, where every write failed with InsufficientPrivilege
--   while the consumer group reported Stable at LAG 0 and committed its
--   offsets anyway.
--
--   scripts/validation/check_topology_grant_delivery.py is the gate that makes
--   this a bound rather than a comment, and it refused this relation in both
--   of its arms before this file existed.
--
--   The grant rides in the OWNING node's lineage, beside the file that creates
--   the relation -- the node_projection_session_replay/0002,
--   node_projection_tenant_registry/0001 and node_projection_receipt_gate/0001
--   convention -- and deliberately NOT in a shared cross-node grant file. A
--   shared file is how a relation added to a node later silently misses its
--   grant, and OMN-15701 is that failure: one pin regeneration reverted eight
--   grants at once because they all lived in one place.
--
-- ============================================================================
-- WHY DELETE, WHICH NO OTHER PROJECTION GRANT CARRIES
-- ============================================================================
--   The platform's derived write set is SELECT/INSERT/UPDATE, on the stated
--   invariant that "a projection writer upserts, it does not reshape the
--   table". This relation is the one place that invariant does not hold, and
--   the reason is the read model's whole point.
--
--   The emitter publishes the WHOLE fleet every cycle, which is what makes an
--   ABSENCE meaningful: a runner this observing host reported last cycle and
--   does not report now has been DEREGISTERED. An upsert cannot express that.
--   Leaving the row behind would leave a deregistered runner reporting
--   `online` forever and a capacity panel counting it -- worse than no row,
--   and exactly the false-green this whole ticket exists to remove. So the
--   writer issues a real DELETE, scoped to the observing host so one observer
--   can never deregister another's runners, and publishes a real Kafka
--   tombstone with it so the bus-backed cache reclaims the key too.
--
--   The privilege is granted here rather than by widening
--   omnibase_infra's WRITE_PRIVILEGES derivation: that constant is the
--   platform's settled stance for every other projection, and one relation
--   with a genuinely different lifecycle is not a reason to hand DELETE to
--   every projection writer in the fleet.
--
--   TRUNCATE is NOT granted. DELETE removes a named row the observation
--   proves is gone; TRUNCATE would let a bug empty the fleet.
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
--     runner_fleet_liveness_projection_cursor_seq
--
--   (An IDENTITY column would not need this -- its sequence is owned by the
--   column and rides the table's INSERT privilege. This is BIGSERIAL, which is
--   precisely the distinction.) This is OMN-17447's class, and
--   check_topology_grant_delivery.py DERIVES the requirement from the applied
--   corpus rather than from a hand list, so a new BIGSERIAL projection cannot
--   ship without either its grant or a gate failure.
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
GRANT SELECT, INSERT, UPDATE, DELETE
    ON omninode_internal.runner_fleet_liveness
    TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 3. Sequence grant behind the BIGSERIAL projection_cursor.
--
--    The sequence is named STATICALLY, not resolved through
--    pg_get_serial_sequence inside a procedural block. omnibase_infra's
--    OMN-15361 SQL gate refuses a DO block carrying dynamic SQL, on the
--    correct ground that it cannot prove which relations such a block touches
--    -- an ownership gate that let `EXECUTE format(...)` through would be
--    trivially bypassable. The literal spelling is safe HERE specifically
--    because 0000 in this same lineage creates the column as BIGSERIAL and
--    nothing renames it: PostgreSQL's <table>_<column>_seq default is not a
--    guess about an unknown database, it is this lineage's own output. The
--    assertion below proves the grant landed on the sequence that actually
--    backs the column, so a spelling that ever stopped matching fails the
--    migration rather than granting nothing quietly.
-- ---------------------------------------------------------------------------
GRANT USAGE
    ON SEQUENCE omninode_internal.runner_fleet_liveness_projection_cursor_seq
    TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 4. Assertions: fail the migration if a grant did not take.
--
--    Every privilege is asserted, not just INSERT. Asserting only the table
--    INSERT is exactly what let the broken state ship for pr_merged_events --
--    it was TRUE for the whole 24-day outage while the sequence privilege,
--    the one actually missing, was never checked.
-- ---------------------------------------------------------------------------
SELECT 1 / count(*) AS runner_fleet_liveness_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'runner_fleet_liveness'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS runner_fleet_liveness_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'runner_fleet_liveness'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS runner_fleet_liveness_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'runner_fleet_liveness'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';

SELECT 1 / count(*) AS runner_fleet_liveness_delete_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'runner_fleet_liveness'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'DELETE';

SELECT 1 / count(*) AS runner_fleet_liveness_cursor_sequence_usage_assertion
WHERE has_sequence_privilege(
          'omninode_runtime',
          pg_get_serial_sequence(
              'omninode_internal.runner_fleet_liveness', 'projection_cursor'
          ),
          'USAGE'
      );
