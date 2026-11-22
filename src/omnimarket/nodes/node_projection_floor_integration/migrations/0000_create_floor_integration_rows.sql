-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT

-- OMN-25103: the newest floor integration pass, one row per merged change.
CREATE TABLE IF NOT EXISTS omninode_internal.floor_integration_rows (
    key TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    number INTEGER NOT NULL CHECK (number > 0),
    title TEXT NOT NULL,
    merged_at TEXT NOT NULL,
    target TEXT NOT NULL,
    state TEXT NOT NULL,
    gap_class TEXT NOT NULL,
    step TEXT,
    running_path TEXT,
    running_line TEXT,
    merged INTEGER NOT NULL CHECK (merged >= 0),
    seen_running INTEGER NOT NULL CHECK (seen_running >= 0),
    settled_elsewhere INTEGER NOT NULL CHECK (settled_elsewhere >= 0),
    reading_owed INTEGER NOT NULL CHECK (reading_owed >= 0),
    unintegrated INTEGER NOT NULL CHECK (unintegrated >= 0),
    as_of TEXT NOT NULL,
    projected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- CREATE TABLE IF NOT EXISTS is a no-op against a pre-existing table, so each
-- declared column is reconciled on its own.
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS key TEXT;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS repository TEXT;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS number INTEGER;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS merged_at TEXT;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS target TEXT;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS state TEXT;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS gap_class TEXT;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS step TEXT;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS running_path TEXT;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS running_line TEXT;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS merged INTEGER;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS seen_running INTEGER;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS settled_elsewhere INTEGER;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS reading_owed INTEGER;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS unintegrated INTEGER;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS as_of TEXT;
ALTER TABLE omninode_internal.floor_integration_rows
    ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_floor_integration_rows_as_of
    ON omninode_internal.floor_integration_rows (as_of);
