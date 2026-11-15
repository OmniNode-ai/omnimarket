-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- =============================================================================
-- MIGRATION: omninode_internal.work_ledger_rows provisional ledger_seq
-- =============================================================================
-- Ticket:  OMN-24634
-- Owner:   omnimarket.nodes.node_projection_work_ledger
--
-- The judge owns ledger_seq. A live row that reaches the projection with none is given the next
-- number after the ledger's tip so it lands in order, and the row is marked here: that number is
-- the projection's, provisional, and gives way when the judge states the same number on a row it
-- accepts (the projection moves its own row; the judge's sequence is never refused). A row the
-- judge sequenced, and every row that predates this column, is not provisional.
--
-- Constant default, so the existing rows are not rewritten. No new relation, no grant: the table's
-- existing grant covers the column.
-- =============================================================================

ALTER TABLE omninode_internal.work_ledger_rows
    ADD COLUMN IF NOT EXISTS ledger_seq_provisional BOOLEAN NOT NULL DEFAULT FALSE;
