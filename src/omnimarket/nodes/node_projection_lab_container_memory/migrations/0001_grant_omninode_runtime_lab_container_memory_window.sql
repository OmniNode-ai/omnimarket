-- OMN-19961: omninode_runtime grants for omninode_internal.lab_container_memory_window.
-- Target DB: omnidash_analytics (NODE_POSTGRES_DB)
-- Node: node_projection_lab_container_memory
--
-- WHY A GRANT FILE AT ALL
--   The topology DECLARES the grant (its TABLE list is generated from this
--   node's db_io block), but declaring a grant is not issuing one. Without
--   this file every write fails with InsufficientPrivilege while the consumer
--   group reports Stable and commits its offsets (OMN-17377, OMN-17379). The
--   grant rides in the owning node's lineage, beside the file that creates
--   the relation, never in a shared cross-node grant file (OMN-15701).
--
-- WHY SELECT, INSERT, UPDATE AND NOTHING ELSE
--   The writer upserts: INSERT, UPDATE on conflict, and SELECT, which Postgres
--   checks for the ON CONFLICT target and the RETURNING column. It never
--   deletes a window: a window is an observation, and the event log it came
--   from is kept. The table has no BIGSERIAL, so there is no sequence grant.
--
-- Idempotency: GRANT is idempotent; re-running is a no-op.

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.lab_container_memory_window
    TO omninode_runtime;

-- Assertions: fail the migration if a grant did not take.
SELECT 1 / count(*) AS lab_container_memory_window_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'lab_container_memory_window'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS lab_container_memory_window_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'lab_container_memory_window'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS lab_container_memory_window_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'lab_container_memory_window'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';
