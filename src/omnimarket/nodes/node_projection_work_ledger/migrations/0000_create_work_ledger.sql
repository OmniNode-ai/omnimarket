-- =============================================================================
-- MIGRATION: omninode_internal.work_ledger_rows and work_ledger_state
-- =============================================================================
-- Ticket:  OMN-19513 (stage 1 of retiring the manual markdown work ledger)
-- Owner:   omnimarket.nodes.node_projection_work_ledger
--
-- work_ledger_rows  the whole log, one row per ledger row, keyed on the row's
--                   content hash (sha256 of the raw row). A redelivery is
--                   ON CONFLICT DO NOTHING. It exists so the parity check can
--                   compare identical strings and so the markdown file can be
--                   rebuilt from the log.
-- work_ledger_state one row per entity: an open claim (claim:<lane>), a live
--                   hold with its scope (hold:<id>), an unanswered message
--                   (msg:<id>), a ruling or a consent. An opening row writes
--                   the descriptor columns and opened_at, a closing row writes
--                   closed_at, each under an ON CONFLICT ... WHERE guard on
--                   (at, row_id), so a redelivered or reordered event cannot
--                   move an entity backwards. is_open is GENERATED from the two
--                   column groups; nothing can write it.
--
-- No DELETE anywhere: an entity leaves the open set by a recorded closing row.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.work_ledger_rows (
    row_id        TEXT        NOT NULL,
    ledger_id     TEXT        NOT NULL,
    row_ts        TIMESTAMPTZ NOT NULL,
    row_type      TEXT        NOT NULL,
    row_lane      TEXT,
    tickets       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    raw_row       TEXT        NOT NULL,
    source        TEXT        NOT NULL DEFAULT 'unknown',
    projected_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (row_id)
);

CREATE TABLE IF NOT EXISTS omninode_internal.work_ledger_state (
    entity_key      TEXT        NOT NULL,
    kind            TEXT        NOT NULL,
    lane            TEXT,
    ticket          TEXT,
    repo            TEXT,
    pr              TEXT,
    scope_to        TEXT,
    scope_surface   TEXT,
    until_at        TIMESTAMPTZ,
    detail          TEXT,
    opened_at       TIMESTAMPTZ,
    opened_row_id   TEXT,
    closed_at       TIMESTAMPTZ,
    closed_row_id   TEXT,
    is_open         BOOLEAN GENERATED ALWAYS AS (
        opened_at IS NOT NULL AND (closed_at IS NULL OR closed_at < opened_at)
    ) STORED,
    projected_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (entity_key)
);

CREATE INDEX IF NOT EXISTS idx_work_ledger_rows_ts
    ON omninode_internal.work_ledger_rows (row_ts);

CREATE INDEX IF NOT EXISTS idx_work_ledger_state_open
    ON omninode_internal.work_ledger_state (kind) WHERE is_open;

-- ---- BEGIN OMN-15376 shape reconciliation: work_ledger_rows ----
ALTER TABLE omninode_internal.work_ledger_rows ADD COLUMN IF NOT EXISTS row_id TEXT;
ALTER TABLE omninode_internal.work_ledger_rows ADD COLUMN IF NOT EXISTS ledger_id TEXT;
ALTER TABLE omninode_internal.work_ledger_rows ADD COLUMN IF NOT EXISTS row_ts TIMESTAMPTZ;
ALTER TABLE omninode_internal.work_ledger_rows ADD COLUMN IF NOT EXISTS row_type TEXT;
ALTER TABLE omninode_internal.work_ledger_rows ADD COLUMN IF NOT EXISTS row_lane TEXT;
ALTER TABLE omninode_internal.work_ledger_rows ADD COLUMN IF NOT EXISTS tickets JSONB DEFAULT '[]'::jsonb;
ALTER TABLE omninode_internal.work_ledger_rows ADD COLUMN IF NOT EXISTS raw_row TEXT;
ALTER TABLE omninode_internal.work_ledger_rows ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'unknown';
ALTER TABLE omninode_internal.work_ledger_rows ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ;
-- ---- END OMN-15376 shape reconciliation: work_ledger_rows ----

-- ---- BEGIN OMN-15376 shape reconciliation: work_ledger_state ----
ALTER TABLE omninode_internal.work_ledger_state ADD COLUMN IF NOT EXISTS entity_key TEXT;
ALTER TABLE omninode_internal.work_ledger_state ADD COLUMN IF NOT EXISTS kind TEXT;
ALTER TABLE omninode_internal.work_ledger_state ADD COLUMN IF NOT EXISTS lane TEXT;
ALTER TABLE omninode_internal.work_ledger_state ADD COLUMN IF NOT EXISTS ticket TEXT;
ALTER TABLE omninode_internal.work_ledger_state ADD COLUMN IF NOT EXISTS repo TEXT;
ALTER TABLE omninode_internal.work_ledger_state ADD COLUMN IF NOT EXISTS pr TEXT;
ALTER TABLE omninode_internal.work_ledger_state ADD COLUMN IF NOT EXISTS scope_to TEXT;
ALTER TABLE omninode_internal.work_ledger_state ADD COLUMN IF NOT EXISTS scope_surface TEXT;
ALTER TABLE omninode_internal.work_ledger_state ADD COLUMN IF NOT EXISTS until_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.work_ledger_state ADD COLUMN IF NOT EXISTS detail TEXT;
ALTER TABLE omninode_internal.work_ledger_state ADD COLUMN IF NOT EXISTS opened_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.work_ledger_state ADD COLUMN IF NOT EXISTS opened_row_id TEXT;
ALTER TABLE omninode_internal.work_ledger_state ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.work_ledger_state ADD COLUMN IF NOT EXISTS closed_row_id TEXT;
ALTER TABLE omninode_internal.work_ledger_state ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ;
-- ---- END OMN-15376 shape reconciliation: work_ledger_state ----
