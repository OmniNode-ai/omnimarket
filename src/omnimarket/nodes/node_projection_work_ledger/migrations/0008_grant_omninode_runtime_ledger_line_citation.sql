-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- =============================================================================
-- MIGRATION: grant the runtime role its set on ledger_line_citation
-- =============================================================================
-- Ticket: OMN-21947. Split from 0007 (as 0001 is split from 0000) so the DDL
-- migration applies to a throwaway schema in the real-Postgres test without the
-- runtime role existing there.
-- The projection contract declares the relation with write access, and the
-- runtime refuses to wire a binding whose principal lacks SELECT, INSERT and
-- UPDATE on it. UPDATE is granted for that declared set only: the
-- ledger_line_citation_immutable trigger from 0007 refuses every UPDATE and
-- DELETE, so a granted UPDATE changes nothing. DELETE is deliberately not granted.
-- =============================================================================

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.ledger_line_citation
    TO omninode_runtime;

-- Check effective privileges too: an inherited DELETE must not pass merely
-- because this migration's direct GRANT omitted it.
SELECT 1 / (
    has_table_privilege('omninode_runtime', 'omninode_internal.ledger_line_citation', 'SELECT')
    AND has_table_privilege('omninode_runtime', 'omninode_internal.ledger_line_citation', 'INSERT')
    AND has_table_privilege('omninode_runtime', 'omninode_internal.ledger_line_citation', 'UPDATE')
    AND NOT has_table_privilege('omninode_runtime', 'omninode_internal.ledger_line_citation', 'DELETE')
)::int AS ledger_line_citation_write_grant_assertion;
