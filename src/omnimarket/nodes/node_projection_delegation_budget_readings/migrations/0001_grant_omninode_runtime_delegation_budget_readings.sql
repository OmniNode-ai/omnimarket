-- OMN-21326 AC3: runtime grants for omninode_internal.delegation_budget_readings.
-- The writer reads the stored row for a correlation id and upserts the newer
-- terminal; the readback reads the window through the same runtime role.

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.delegation_budget_readings
    TO omninode_runtime;

GRANT USAGE
    ON SEQUENCE omninode_internal.delegation_budget_readings_projection_cursor_seq
    TO omninode_runtime;

SELECT 1 / count(*) AS delegation_budget_readings_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'delegation_budget_readings'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS delegation_budget_readings_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'delegation_budget_readings'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS delegation_budget_readings_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'delegation_budget_readings'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';

SELECT 1 / count(*) AS delegation_budget_readings_cursor_sequence_usage_assertion
WHERE has_sequence_privilege(
          'omninode_runtime',
          pg_get_serial_sequence(
              'omninode_internal.delegation_budget_readings', 'projection_cursor'
          ),
          'USAGE'
      );
