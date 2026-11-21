-- =============================================================================
-- MIGRATION: grant the runtime role its write set on the ticket hand-edit tables
-- =============================================================================
-- Ticket: OMN-23776. Split from 0001 so the DDL can be applied to a throwaway database
-- without the role. SELECT, INSERT, UPDATE and deliberately NOT DELETE: the store keeps
-- its rows (written rows are replaced, never removed; findings are closed, never deleted).
-- =============================================================================

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.ticket_hand_edit_written,
       omninode_internal.ticket_hand_edit_cursor,
       omninode_internal.ticket_hand_edit_open_findings
    TO omninode_runtime;

SELECT 1 / (count(DISTINCT table_name) / 3) AS ticket_hand_edit_write_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name IN (
      'ticket_hand_edit_written',
      'ticket_hand_edit_cursor',
      'ticket_hand_edit_open_findings'
  )
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';
