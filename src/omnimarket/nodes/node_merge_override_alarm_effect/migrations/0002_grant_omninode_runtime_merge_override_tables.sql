-- =============================================================================
-- MIGRATION: grant the runtime role its write set on the merge override tables
-- =============================================================================
-- Ticket: OMN-23836. Split from 0001, as the overlay store splits its grant, so the DDL can be
-- applied to a throwaway database without the role. SELECT, INSERT, UPDATE and deliberately NOT
-- DELETE: the store keeps its rows (receipts are replaced, never removed; findings are opened,
-- never deleted). The omnibase_infra topology grants the same set (OMN-23858).
-- =============================================================================

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.merge_override_receipts,
       omninode_internal.merge_override_pending,
       omninode_internal.merge_override_verdicts,
       omninode_internal.merge_override_open_findings
    TO omninode_runtime;

SELECT 1 / (count(DISTINCT table_name) / 4) AS merge_override_write_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name IN (
      'merge_override_receipts',
      'merge_override_pending',
      'merge_override_verdicts',
      'merge_override_open_findings'
  )
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';
