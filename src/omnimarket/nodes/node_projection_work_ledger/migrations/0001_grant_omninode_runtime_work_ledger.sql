-- =============================================================================
-- MIGRATION: grant the runtime role its write set on the work-ledger tables
-- =============================================================================
-- Ticket: OMN-19513. Split from 0000 (as the lab lane-health projection splits
-- its grant) so the DDL migration can be applied to a throwaway schema in the
-- real-Postgres write-path test without the runtime role existing there.
-- SELECT, INSERT, UPDATE and deliberately NOT DELETE: an entity leaves the open
-- set by a recorded closing row, never by removal.
-- =============================================================================

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.work_ledger_rows
    TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.work_ledger_state
    TO omninode_runtime;

SELECT 1 / count(*) AS work_ledger_rows_write_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'work_ledger_rows'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS work_ledger_state_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'work_ledger_state'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';
