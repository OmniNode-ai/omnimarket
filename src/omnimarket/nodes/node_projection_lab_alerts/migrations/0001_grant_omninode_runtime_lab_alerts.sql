-- omninode_runtime grants for omninode_internal.lab_alerts.
-- Target DB: omnidash_analytics (NODE_POSTGRES_DB)
-- Node: node_projection_lab_alerts
--
-- The grant rides in the owning node's lineage beside the file that creates the
-- relation. SELECT/INSERT/UPDATE is the write set for a projection writer that
-- upserts and does not reshape the table. No DELETE: a cleared alert stays as a
-- cleared row. No sequence grant: the key is alert_id and there is no BIGSERIAL.
-- GRANT is idempotent; re-running is a no-op.

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.lab_alerts
    TO omninode_runtime;

SELECT 1 / count(*) AS lab_alerts_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'lab_alerts'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS lab_alerts_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'lab_alerts'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS lab_alerts_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'lab_alerts'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';
