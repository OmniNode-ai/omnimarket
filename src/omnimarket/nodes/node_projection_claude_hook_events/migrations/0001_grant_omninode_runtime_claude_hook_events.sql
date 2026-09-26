-- =============================================================================
-- MIGRATION: omninode_runtime grants on claude_hook_events and claude_agent_spans
-- =============================================================================
-- Ticket: OMN-19513. Runs after 0000's shape assertions, so a table 0000
-- rejected never receives a write grant.
--
-- The projection writes under the omninode_runtime binding, so that role gets
-- exactly the writer's scope: SELECT (the parent lookup and the tool-call
-- recount), INSERT and UPDATE (the upsert and the out-of-order repair). No
-- DELETE: a captured hook event is never deregistered, and the node declares
-- `access: read_write`, which does not include it.
--
-- The sequence grants are separate on purpose: a table grant does not reach
-- the BIGSERIAL sequence behind projection_cursor, and without USAGE on it
-- every INSERT fails with a permission error on the sequence -- a write path
-- that looks granted and writes nothing.
--
-- Schema USAGE is the minimum schema privilege and only lets the role resolve
-- omninode_internal.* by name; it grants nothing on any relation in the schema
-- (no CREATE, and no ON ALL TABLES IN SCHEMA). The topology instances give the
-- omninode_runtime principal this same set, so the migration and the topology
-- converge on one scope.
--
-- Every privilege issued is asserted, not merely declared, the schema USAGE
-- included: a missing grant and a writer wired wrongly produce the same silent
-- zero rows.
-- =============================================================================

SELECT 1 / count(*) AS omninode_runtime_role_exists_precondition
  FROM pg_catalog.pg_roles
 WHERE rolname = 'omninode_runtime';

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

SELECT 1 / count(*) AS omninode_runtime_schema_usage_grant_assertion
 WHERE has_schema_privilege('omninode_runtime', 'omninode_internal', 'USAGE');

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.claude_hook_events
    TO omninode_runtime;
GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.claude_agent_spans
    TO omninode_runtime;

GRANT USAGE
    ON SEQUENCE omninode_internal.claude_hook_events_projection_cursor_seq
    TO omninode_runtime;
GRANT USAGE
    ON SEQUENCE omninode_internal.claude_agent_spans_projection_cursor_seq
    TO omninode_runtime;

SELECT 1 / count(*) AS claude_hook_events_select_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS claude_hook_events_insert_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS claude_hook_events_update_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'UPDATE';

SELECT 1 / count(*) AS claude_agent_spans_select_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_agent_spans'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS claude_agent_spans_insert_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_agent_spans'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS claude_agent_spans_update_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_agent_spans'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'UPDATE';

SELECT 1 / count(*) AS claude_hook_events_cursor_sequence_usage_assertion
 WHERE has_sequence_privilege(
           'omninode_runtime',
           pg_get_serial_sequence(
               'omninode_internal.claude_hook_events', 'projection_cursor'
           ),
           'USAGE'
       );

SELECT 1 / count(*) AS claude_agent_spans_cursor_sequence_usage_assertion
 WHERE has_sequence_privilege(
           'omninode_runtime',
           pg_get_serial_sequence(
               'omninode_internal.claude_agent_spans', 'projection_cursor'
           ),
           'USAGE'
       );
