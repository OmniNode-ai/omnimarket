-- OMN-25103: runtime grants for omninode_internal.floor_integration_rows.
-- The writer upserts the newest pass's rows and deletes older passes' rows; the projection read serves them.

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE, DELETE
    ON omninode_internal.floor_integration_rows
    TO omninode_runtime;

SELECT 1 / count(*) AS floor_integration_rows_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'floor_integration_rows'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS floor_integration_rows_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'floor_integration_rows'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS floor_integration_rows_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'floor_integration_rows'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';

SELECT 1 / count(*) AS floor_integration_rows_delete_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'floor_integration_rows'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'DELETE';
