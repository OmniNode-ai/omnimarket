-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- Ticket: OMN-23606. Split from 0011 so the DDL can be applied to a throwaway schema without the
-- runtime role. The writer inserts and updates an entry (newest row wins) and never removes one;
-- DELETE is deliberately not granted.
GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.work_ledger_latest
    TO omninode_runtime;

SELECT 1 / (
    has_table_privilege('omninode_runtime', 'omninode_internal.work_ledger_latest', 'SELECT')
    AND has_table_privilege('omninode_runtime', 'omninode_internal.work_ledger_latest', 'INSERT')
    AND has_table_privilege('omninode_runtime', 'omninode_internal.work_ledger_latest', 'UPDATE')
    AND NOT has_table_privilege('omninode_runtime', 'omninode_internal.work_ledger_latest', 'DELETE')
)::int AS work_ledger_latest_write_grant_assertion;
