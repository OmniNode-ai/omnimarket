-- OMN-26708: omninode_runtime grants for omninode_internal.lane_work_workflow
-- and its terminal-rows view.
-- Target DB: omnidash_analytics (NODE_POSTGRES_DB)
-- Node: node_projection_lane_work
--
-- The topology declares these grants but declaring is not issuing: this file
-- rides in the owning node's lineage beside the migration that creates the
-- relation, as for node_projection_lab_lane_health.
--
-- SELECT/INSERT/UPDATE is the write set for a projection writer that upserts.
-- There is no DELETE (a workflow's row is never removed) and no sequence grant
-- (the key is the workflow id, not a BIGSERIAL).
-- Idempotency: GRANT is idempotent; re-running is a no-op.

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 2. Table grants. SELECT/INSERT/UPDATE is the platform's derived write set
--    for a projection writer that upserts and does not reshape the table,
--    which is exactly what this writer does.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.lane_work_workflow
    TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 3. Assertions: fail the migration if a grant did not take.
--
--    Every privilege is asserted, not just INSERT. Asserting only the table
--    INSERT is exactly what let the broken state ship for pr_merged_events --
--    it was TRUE for the whole 24-day outage while the privilege that was
--    actually missing was never checked.
-- ---------------------------------------------------------------------------
SELECT 1 / count(*) AS lane_work_workflow_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'lane_work_workflow'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS lane_work_workflow_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'lane_work_workflow'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS lane_work_workflow_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'lane_work_workflow'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';

GRANT SELECT ON omninode_internal.lane_work_terminal TO omninode_runtime;
