-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- Ticket: OMN-23152. Split from 0007 so the DDL can be applied to a throwaway schema without the
-- runtime role. SELECT and INSERT only: a recorded conflict is never updated or removed.
GRANT SELECT, INSERT
    ON omninode_internal.work_ledger_seq_conflicts
    TO omninode_runtime;

SELECT 1 / count(*) AS work_ledger_seq_conflicts_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'work_ledger_seq_conflicts'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';
