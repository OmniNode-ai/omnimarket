-- OMN-25103: runtime grants for omninode_internal.floor_integration_pass.
-- The writer reads the held pass and upserts it in the same transaction as the rows.

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.floor_integration_pass
    TO omninode_runtime;

SELECT 1 / count(*) AS floor_integration_pass_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'floor_integration_pass'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS floor_integration_pass_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'floor_integration_pass'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS floor_integration_pass_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'floor_integration_pass'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';
