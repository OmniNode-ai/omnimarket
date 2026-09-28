-- OMN-19833: omninode_runtime grants for the two PR landing read models.
-- Target DB: omnidash_analytics (NODE_POSTGRES_DB)
-- Node: node_projection_pr_landing
--
-- Declaring a grant in the topology is not issuing one (OMN-17377, OMN-17379:
-- projections at zero rows while every write failed with InsufficientPrivilege
-- and the consumer committed its offsets anyway). The grants ride in the owning
-- node's lineage, beside the file that creates the relations.
--
-- pr_landing_state gets SELECT/INSERT/UPDATE: it is upserted per PR.
-- pr_landing_transitions gets SELECT/INSERT only: it is append-only, and a
-- grant that withholds UPDATE is what makes that a property of the database
-- rather than of the writer's good behaviour. Neither gets DELETE.
--
-- Each BIGSERIAL projection_cursor is a standalone sequence whose own acl is
-- checked on every INSERT; a table grant does not reach it (OMN-17447). The
-- sequences are named statically because the OMN-15361 SQL gate refuses a DO
-- block with dynamic SQL; the assertions below prove each grant landed on the
-- sequence that actually backs its column.
--
-- Idempotency: GRANT is idempotent. Nothing here touches RLS, ownership, or
-- any role attribute.

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.pr_landing_state
    TO omninode_runtime;

GRANT SELECT, INSERT
    ON omninode_internal.pr_landing_transitions
    TO omninode_runtime;

GRANT USAGE
    ON SEQUENCE omninode_internal.pr_landing_state_projection_cursor_seq
    TO omninode_runtime;

GRANT USAGE
    ON SEQUENCE omninode_internal.pr_landing_transitions_projection_cursor_seq
    TO omninode_runtime;

-- Assertions: fail the migration if a grant did not take.
SELECT 1 / count(*) AS pr_landing_state_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'pr_landing_state'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS pr_landing_state_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'pr_landing_state'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS pr_landing_state_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'pr_landing_state'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';

SELECT 1 / count(*) AS pr_landing_transitions_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'pr_landing_transitions'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS pr_landing_transitions_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'pr_landing_transitions'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS pr_landing_state_cursor_sequence_usage_assertion
WHERE has_sequence_privilege(
          'omninode_runtime',
          pg_get_serial_sequence(
              'omninode_internal.pr_landing_state', 'projection_cursor'
          ),
          'USAGE'
      );

SELECT 1 / count(*) AS pr_landing_transitions_cursor_sequence_usage_assertion
WHERE has_sequence_privilege(
          'omninode_runtime',
          pg_get_serial_sequence(
              'omninode_internal.pr_landing_transitions', 'projection_cursor'
          ),
          'USAGE'
      );
