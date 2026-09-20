-- OMN-18769: omninode_runtime grants for omninode_internal.lab_lane_health.
-- Target DB: omnidash_analytics (NODE_POSTGRES_DB)
-- Node: node_projection_lab_lane_health
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
--   this a bound rather than a comment, and it refused this relation before
--   this file existed: "UNDELIVERED omninode_runtime ->
--   omninode_internal.lab_lane_health", over the ratchet bound.
--
--   The grant rides in the OWNING node's lineage, beside the file that creates
--   the relation, and deliberately NOT in a shared cross-node grant file. A
--   shared file is how a relation added to a node later silently misses its
--   grant, and OMN-15701 is that failure: one pin regeneration reverted eight
--   grants at once because they all lived in one place.
--
-- ============================================================================
-- WHY THIS ONE IS NARROWER THAN THE RUNNER-FLEET GRANT BESIDE IT
-- ============================================================================
--   Two privileges that sibling carries are deliberately absent here, and in
--   both cases the reason is a property of this read model rather than an
--   oversight.
--
--   NO DELETE. The runner-fleet emitter publishes the whole fleet each cycle,
--   so an ABSENCE there means a runner was deregistered and the writer must
--   remove the row. This relation is the opposite shape: one row per lab lane,
--   keyed on `lane`, updated in place as each of its three facts is observed
--   independently. A lane that is not reported this cycle has not been
--   deleted, it simply has no new fact -- and the row's per-fact observed_at
--   columns are what express that, which is the whole point of aging the
--   three facts separately. Granting DELETE would let a partial observation
--   erase a lane that is merely quiet.
--
--   NO SEQUENCE GRANT. The sequence half exists for a BIGSERIAL cursor, whose
--   standalone sequence carries its own acl that a table grant does not reach
--   (OMN-17447). This table has no BIGSERIAL column and no projection_cursor
--   at all -- its key is `lane` and its ordering is the per-fact observed_at
--   columns -- so there is no sequence to grant on. Adding one would name an
--   object that does not exist and fail the migration.
--
-- Idempotency: GRANT is idempotent; re-running is a no-op. Nothing here
-- touches RLS, ownership, or any role attribute.

-- ---------------------------------------------------------------------------
-- 1. Schema USAGE, mirroring the topology's SCHEMA grant. Re-asserted rather
--    than assumed: a migration must not depend on a sibling file having run.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 2. Table grants. SELECT/INSERT/UPDATE is the platform's derived write set
--    for a projection writer that upserts and does not reshape the table,
--    which is exactly what this writer does.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.lab_lane_health
    TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 3. Assertions: fail the migration if a grant did not take.
--
--    Every privilege is asserted, not just INSERT. Asserting only the table
--    INSERT is exactly what let the broken state ship for pr_merged_events --
--    it was TRUE for the whole 24-day outage while the privilege that was
--    actually missing was never checked.
-- ---------------------------------------------------------------------------
SELECT 1 / count(*) AS lab_lane_health_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'lab_lane_health'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS lab_lane_health_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'lab_lane_health'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS lab_lane_health_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'lab_lane_health'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';
