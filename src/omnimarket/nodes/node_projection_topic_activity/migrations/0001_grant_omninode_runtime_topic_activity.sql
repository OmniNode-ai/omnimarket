-- OMN-19716: runtime grants for omninode_internal.topic_activity.
-- The writer reads prior rows and upserts current or absent rows.

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.topic_activity
    TO omninode_runtime;

GRANT USAGE
    ON SEQUENCE omninode_internal.topic_activity_projection_cursor_seq
    TO omninode_runtime;

SELECT 1 / count(*) AS topic_activity_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'topic_activity'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS topic_activity_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'topic_activity'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS topic_activity_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'topic_activity'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';

SELECT 1 / count(*) AS topic_activity_cursor_sequence_usage_assertion
WHERE has_sequence_privilege(
          'omninode_runtime',
          pg_get_serial_sequence(
              'omninode_internal.topic_activity', 'projection_cursor'
          ),
          'USAGE'
      );
