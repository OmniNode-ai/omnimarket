-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-19999. Grants separate from DDL for isolated-schema write-path tests.
-- Closed and merged observations are retained; no DELETE privilege.
GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;
GRANT SELECT, INSERT, UPDATE ON omninode_internal.pr_state TO omninode_runtime;

SELECT 1 / count(*) AS pr_state_write_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'pr_state'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS pr_state_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'pr_state'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';
