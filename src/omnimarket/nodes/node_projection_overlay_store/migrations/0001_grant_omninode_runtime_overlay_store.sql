-- =============================================================================
-- MIGRATION: grant the runtime role its write set on the overlay store
-- =============================================================================
-- Ticket: OMN-22478. Split from 0000, as the work ledger splits its grant, so the DDL
-- can be applied to a throwaway database in the real-Postgres test without the role.
-- SELECT, INSERT, UPDATE and deliberately NOT DELETE: a version is never removed.
-- =============================================================================

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.overlay_store
    TO omninode_runtime;

SELECT 1 / count(*) AS overlay_store_write_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'overlay_store'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';
