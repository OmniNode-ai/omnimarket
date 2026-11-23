-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT

CREATE TABLE IF NOT EXISTS omninode_internal.done_items (
    ticket TEXT PRIMARY KEY,
    done_at TEXT NOT NULL,
    done_row_id TEXT NOT NULL CHECK (done_row_id ~ '^[0-9a-f]{64}$'),
    lane TEXT,
    pr TEXT,
    closes TEXT,
    projected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- CREATE TABLE IF NOT EXISTS is a no-op against a pre-existing table, so each
-- declared column is reconciled on its own.
ALTER TABLE omninode_internal.done_items
    ADD COLUMN IF NOT EXISTS ticket TEXT;
ALTER TABLE omninode_internal.done_items
    ADD COLUMN IF NOT EXISTS done_at TEXT;
ALTER TABLE omninode_internal.done_items
    ADD COLUMN IF NOT EXISTS done_row_id TEXT;
ALTER TABLE omninode_internal.done_items
    ADD COLUMN IF NOT EXISTS lane TEXT;
ALTER TABLE omninode_internal.done_items
    ADD COLUMN IF NOT EXISTS pr TEXT;
ALTER TABLE omninode_internal.done_items
    ADD COLUMN IF NOT EXISTS closes TEXT;
ALTER TABLE omninode_internal.done_items
    ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_done_items_projected_at
    ON omninode_internal.done_items (projected_at DESC);
