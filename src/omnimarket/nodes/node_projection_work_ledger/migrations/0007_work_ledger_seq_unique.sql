-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- =============================================================================
-- MIGRATION: omninode_internal.work_ledger_rows unique ledger_seq
-- =============================================================================
-- Ticket:  OMN-23152 (rebuild plan 4.3 step 1)
-- Owner:   omnimarket.nodes.node_projection_work_ledger
--
-- 0003 left the (ledger_id, ledger_seq) index non-unique on purpose: a unique violation raised inside
-- the projection consumer is a poison message that stalls every later row. The writer now judges the
-- collision itself before it inserts (a second row for a held sequence goes to
-- work_ledger_seq_conflicts and is never inserted), so the index can refuse what the writer misses.
--
-- A sequence held by more than one row keeps one row: the row the judge's own emit wrote (source
-- 'onex-ledger'), then the earliest projected, then the lowest row_id. Every other row of that
-- sequence is copied whole into work_ledger_seq_conflicts and its ledger_seq is set NULL here; no row
-- is deleted. The history backfill (a ruled step of its own) gives such a row its sequence from file
-- order.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.work_ledger_seq_conflicts (
    row_id       TEXT        NOT NULL,
    ledger_id    TEXT        NOT NULL,
    ledger_seq   BIGINT      NOT NULL,
    held_row_id  TEXT,
    raw_row      TEXT        NOT NULL,
    source       TEXT        NOT NULL DEFAULT 'unknown',
    reason       TEXT        NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (row_id)
);

WITH ranked AS (
    SELECT row_id, ledger_id, ledger_seq, raw_row, source,
           first_value(row_id) OVER ordered AS kept_row_id,
           row_number() OVER ordered AS rank_in_seq
    FROM omninode_internal.work_ledger_rows
    WHERE ledger_seq IS NOT NULL
    WINDOW ordered AS (
        PARTITION BY ledger_id, ledger_seq
        ORDER BY (source <> 'onex-ledger'), projected_at, row_id
    )
), moved AS (
    INSERT INTO omninode_internal.work_ledger_seq_conflicts
        (row_id, ledger_id, ledger_seq, held_row_id, raw_row, source, reason)
    SELECT row_id, ledger_id, ledger_seq, kept_row_id, raw_row, source,
           'duplicate ledger_seq at the unique-index migration'
    FROM ranked
    WHERE rank_in_seq > 1
    ON CONFLICT (row_id) DO NOTHING
    RETURNING row_id
)
UPDATE omninode_internal.work_ledger_rows AS rows
SET ledger_seq = NULL
FROM moved
WHERE rows.row_id = moved.row_id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_work_ledger_rows_seq
    ON omninode_internal.work_ledger_rows (ledger_id, ledger_seq)
    WHERE ledger_seq IS NOT NULL;
