-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- The applying runtime provisions this role; the node owns its table grants.
GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;
GRANT SELECT, INSERT, UPDATE ON omninode_internal.worktree_reconcile_hosts TO omninode_runtime;

SELECT 1 / count(*) AS worktree_reconcile_grants_assertion
FROM (
    SELECT grantee
    FROM information_schema.role_table_grants
    WHERE table_schema = 'omninode_internal'
      AND table_name = 'worktree_reconcile_hosts'
      AND grantee = 'omninode_runtime'
      AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE')
    GROUP BY grantee
    HAVING count(DISTINCT privilege_type) = 3
) AS granted;
