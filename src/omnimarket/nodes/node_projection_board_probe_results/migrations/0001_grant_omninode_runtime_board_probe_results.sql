-- OMN-19937: deliver and assert the runtime grants for the projection table.
GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.board_probe_results
    TO omninode_runtime;

GRANT USAGE
    ON SEQUENCE omninode_internal.board_probe_results_projection_cursor_seq
    TO omninode_runtime;

SELECT 1 / count(*) AS board_probe_results_cursor_sequence_identity_assertion
WHERE pg_get_serial_sequence(
          'omninode_internal.board_probe_results', 'projection_cursor'
      ) = 'omninode_internal.board_probe_results_projection_cursor_seq';

SELECT 1 / count(*) AS board_probe_results_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'board_probe_results'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS board_probe_results_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'board_probe_results'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS board_probe_results_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'board_probe_results'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';

SELECT 1 / count(*) AS board_probe_results_sequence_usage_assertion
WHERE has_sequence_privilege(
          'omninode_runtime',
          'omninode_internal.board_probe_results_projection_cursor_seq',
          'USAGE'
      );
