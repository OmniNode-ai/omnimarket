-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- Ticket: OMN-23152. Split from 0007 so the DDL can be applied to a throwaway schema without the
-- runtime role. The projection contract declares work_ledger_seq_conflicts with write access, and the
-- runtime refuses to wire a binding whose principal lacks SELECT, INSERT and UPDATE on it. The writer only
-- inserts a conflict and never updates or removes one; DELETE is deliberately not granted.
GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.work_ledger_seq_conflicts
    TO omninode_runtime;

-- Check effective privileges too: an inherited DELETE must not pass merely because this
-- migration's direct GRANT omitted it.
SELECT 1 / (
    has_table_privilege('omninode_runtime', 'omninode_internal.work_ledger_seq_conflicts', 'SELECT')
    AND has_table_privilege('omninode_runtime', 'omninode_internal.work_ledger_seq_conflicts', 'INSERT')
    AND has_table_privilege('omninode_runtime', 'omninode_internal.work_ledger_seq_conflicts', 'UPDATE')
    AND NOT has_table_privilege('omninode_runtime', 'omninode_internal.work_ledger_seq_conflicts', 'DELETE')
)::int AS work_ledger_seq_conflicts_write_grant_assertion;
