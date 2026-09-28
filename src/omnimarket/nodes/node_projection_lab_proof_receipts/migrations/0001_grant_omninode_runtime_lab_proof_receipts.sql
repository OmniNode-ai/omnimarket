-- OMN-19566: runtime grants for omninode_internal.lab_proof_receipts.
-- The writer reads the stored row for a key and upserts the newer proof.

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.lab_proof_receipts
    TO omninode_runtime;

GRANT USAGE
    ON SEQUENCE omninode_internal.lab_proof_receipts_projection_cursor_seq
    TO omninode_runtime;

SELECT 1 / count(*) AS lab_proof_receipts_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'lab_proof_receipts'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS lab_proof_receipts_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'lab_proof_receipts'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS lab_proof_receipts_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'lab_proof_receipts'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';

SELECT 1 / count(*) AS lab_proof_receipts_cursor_sequence_usage_assertion
WHERE has_sequence_privilege(
          'omninode_runtime',
          pg_get_serial_sequence(
              'omninode_internal.lab_proof_receipts', 'projection_cursor'
          ),
          'USAGE'
      );
