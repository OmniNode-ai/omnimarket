-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- Owned by omnimarket.nodes.node_projection_worktree_reconcile.
CREATE TABLE IF NOT EXISTS omninode_internal.worktree_reconcile_hosts (
    host TEXT PRIMARY KEY,
    correlation_id UUID NOT NULL,
    scanned BIGINT NOT NULL CHECK (scanned >= 0),
    removed BIGINT NOT NULL CHECK (removed >= 0),
    pinned_and_removed BIGINT NOT NULL CHECK (pinned_and_removed >= 0),
    kept BIGINT NOT NULL CHECK (kept >= 0),
    needs_human BIGINT NOT NULL CHECK (needs_human >= 0),
    failures BIGINT NOT NULL CHECK (failures >= 0),
    freed_bytes BIGINT NOT NULL CHECK (freed_bytes >= 0),
    needs_human_paths TEXT[] NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL
);

-- OMN-15376: These adds are no-ops on a fresh create and converge a drifted pre-existing table.
ALTER TABLE omninode_internal.worktree_reconcile_hosts
    ADD COLUMN IF NOT EXISTS host TEXT;

ALTER TABLE omninode_internal.worktree_reconcile_hosts
    ADD COLUMN IF NOT EXISTS correlation_id UUID;

ALTER TABLE omninode_internal.worktree_reconcile_hosts
    ADD COLUMN IF NOT EXISTS scanned BIGINT;

ALTER TABLE omninode_internal.worktree_reconcile_hosts
    ADD COLUMN IF NOT EXISTS removed BIGINT;

ALTER TABLE omninode_internal.worktree_reconcile_hosts
    ADD COLUMN IF NOT EXISTS pinned_and_removed BIGINT;

ALTER TABLE omninode_internal.worktree_reconcile_hosts
    ADD COLUMN IF NOT EXISTS kept BIGINT;

ALTER TABLE omninode_internal.worktree_reconcile_hosts
    ADD COLUMN IF NOT EXISTS needs_human BIGINT;

ALTER TABLE omninode_internal.worktree_reconcile_hosts
    ADD COLUMN IF NOT EXISTS failures BIGINT;

ALTER TABLE omninode_internal.worktree_reconcile_hosts
    ADD COLUMN IF NOT EXISTS freed_bytes BIGINT;

ALTER TABLE omninode_internal.worktree_reconcile_hosts
    ADD COLUMN IF NOT EXISTS needs_human_paths TEXT[];

ALTER TABLE omninode_internal.worktree_reconcile_hosts
    ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
