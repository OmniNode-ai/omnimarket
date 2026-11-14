-- =============================================================================
-- MIGRATION: omninode_runtime grants on goal_trace_events and goal_traces
-- =============================================================================
-- Ticket: OMN-20034. Runs after 0000's shape assertions, so a table 0000
-- rejected never receives a write grant.
--
-- The projection writes under the omninode_runtime binding, so that role gets
-- exactly the writer's scope: SELECT (the scope read before each fold), INSERT
-- and UPDATE (the event insert and the re-attachment of rows when a binding
-- arrives). No DELETE: a trace event is never deregistered, and the node
-- declares `access: read_write`, which does not include it.
--
-- The sequence grant is separate on purpose: a table grant does not reach the
-- BIGSERIAL sequence behind projection_cursor, and without USAGE on it every
-- INSERT fails with a permission error on the sequence.
-- =============================================================================

SELECT 1 / count(*) AS omninode_runtime_role_exists_precondition
  FROM pg_catalog.pg_roles
 WHERE rolname = 'omninode_runtime';

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.goal_trace_events
    TO omninode_runtime;
GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.goal_traces
    TO omninode_runtime;

GRANT USAGE
    ON SEQUENCE omninode_internal.goal_trace_events_projection_cursor_seq
    TO omninode_runtime;

SELECT 1 / count(*) AS goal_trace_events_insert_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'goal_trace_events'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS goal_trace_events_update_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'goal_trace_events'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'UPDATE';

SELECT 1 / count(*) AS goal_traces_update_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'goal_traces'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'UPDATE';

SELECT 1 / count(*) AS goal_trace_events_cursor_sequence_usage_assertion
 WHERE has_sequence_privilege(
           'omninode_runtime',
           pg_get_serial_sequence(
               'omninode_internal.goal_trace_events', 'projection_cursor'
           ),
           'USAGE'
       );
