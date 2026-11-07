-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- =============================================================================
-- MIGRATION: work-ledger history import (legacy_ordinal) and ledger_line_citation
-- =============================================================================
-- Ticket:  OMN-21947 (work-ledger bus-only cutover, L3)
-- Owner:   omnimarket.nodes.node_projection_work_ledger
--
-- legacy_ordinal: the position of a row in file order (archive splits by date, then the
-- live file) for a row that has no judge-assigned ledger_seq. It is set by the history
-- import and by nothing else. The one ordering contract of every reader and of the L12
-- bootstrap: rows without a ledger_seq first, by legacy_ordinal; then rows with one, by
-- ledger_seq. No existing ledger_seq is read, changed or renumbered here.
--
-- ledger_line_citation: an ACK or amends cell that cites <ledger>:<line> resolves through
-- this immutable table. file_key is a split's file name (it carries the roll date) with
-- generation 0, or 'live' with the roll generation of the live file the citing row was
-- written into (the count of roll blocks that had completed). A row is inserted once
-- and never changed or deleted.
-- =============================================================================

ALTER TABLE omninode_internal.work_ledger_rows ADD COLUMN IF NOT EXISTS legacy_ordinal BIGINT;

-- Unique so a second import that numbered a row differently fails loud. The projection
-- consumer never writes legacy_ordinal, so this cannot poison a live message.
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_ledger_rows_legacy_ordinal
    ON omninode_internal.work_ledger_rows (ledger_id, legacy_ordinal)
    WHERE legacy_ordinal IS NOT NULL;

CREATE TABLE IF NOT EXISTS omninode_internal.ledger_line_citation (
    ledger_id   TEXT   NOT NULL,
    file_key    TEXT   NOT NULL,
    generation  INT    NOT NULL DEFAULT 0,
    line_no     INT    NOT NULL CHECK (line_no >= 1),
    row_id      TEXT   NOT NULL,
    PRIMARY KEY (ledger_id, file_key, generation, line_no)
);

CREATE INDEX IF NOT EXISTS idx_ledger_line_citation_row
    ON omninode_internal.ledger_line_citation (row_id);

CREATE OR REPLACE FUNCTION omninode_internal.ledger_line_citation_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ledger_line_citation is immutable: % refused', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS ledger_line_citation_immutable
    ON omninode_internal.ledger_line_citation;
CREATE TRIGGER ledger_line_citation_immutable
    BEFORE UPDATE OR DELETE ON omninode_internal.ledger_line_citation
    FOR EACH ROW EXECUTE FUNCTION omninode_internal.ledger_line_citation_immutable();
