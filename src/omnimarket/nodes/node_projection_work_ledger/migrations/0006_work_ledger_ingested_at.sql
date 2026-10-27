-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- First database ingestion is distinct from writer time and projection replay.
-- Add without a default first: existing rows retain NULL authority. Only new
-- insertions receive the database clock; conflict/replay leaves it unchanged.
ALTER TABLE omninode_internal.work_ledger_rows
    ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.work_ledger_rows
    ALTER COLUMN ingested_at SET DEFAULT clock_timestamp();
