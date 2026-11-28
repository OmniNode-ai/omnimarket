-- =============================================================================
-- MIGRATION: grant the runtime role its write set on the overlay store's write tables
-- =============================================================================
-- Ticket: OMN-26635. Split from 0002, as 0001 is split from 0000, so the DDL can be applied to a throwaway database in
-- the real-Postgres test without the role. SELECT, INSERT, UPDATE and deliberately NOT DELETE: a write row, a
-- provenance row and an audit row are never removed.
-- =============================================================================

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.overlay_store_revision,
       omninode_internal.overlay_store_write,
       omninode_internal.overlay_store_provenance,
       omninode_internal.overlay_store_audit
    TO omninode_runtime;

SELECT 1 / count(*) AS overlay_store_write_tables_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'overlay_store_audit'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';
