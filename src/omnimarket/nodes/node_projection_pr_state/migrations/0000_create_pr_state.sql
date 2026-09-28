-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-19999. Owner: omnimarket.nodes.node_projection_pr_state
-- Terminal observations remain rows. Digest ordering uses bytewise C collation.

CREATE TABLE IF NOT EXISTS omninode_internal.pr_state (
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    state TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    base TEXT NOT NULL,
    head_ref TEXT NOT NULL,
    title VARCHAR(200) NOT NULL,
    draft BOOLEAN NOT NULL,
    author TEXT NOT NULL,
    author_is_bot BOOLEAN NOT NULL,
    labels JSONB NOT NULL,
    armed BOOLEAN NOT NULL,
    queued BOOLEAN NOT NULL,
    watcher_class TEXT NOT NULL,
    ci_verdict TEXT NOT NULL,
    red_contexts JSONB NOT NULL,
    pending_contexts JSONB NOT NULL,
    ci_read_at TEXT NOT NULL,
    merged_at TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    digest TEXT NOT NULL,
    last_digest TEXT COLLATE "C" NOT NULL,
    PRIMARY KEY (repo, pr_number),
    CHECK (state IN ('open', 'closed', 'merged')),
    CHECK (ci_verdict IN ('GREEN', 'RED', 'PENDING', 'NONE')),
    CHECK (digest = last_digest)
);

-- ---- BEGIN OMN-15376 shape reconciliation: pr_state ----
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS repo TEXT;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS pr_number INTEGER;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS state TEXT;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS head_sha TEXT;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS base TEXT;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS head_ref TEXT;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS title VARCHAR(200);
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS draft BOOLEAN;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS author TEXT;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS author_is_bot BOOLEAN;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS labels JSONB;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS armed BOOLEAN;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS queued BOOLEAN;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS watcher_class TEXT;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS ci_verdict TEXT;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS red_contexts JSONB;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS pending_contexts JSONB;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS ci_read_at TEXT;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS merged_at TEXT;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS digest TEXT;
ALTER TABLE omninode_internal.pr_state ADD COLUMN IF NOT EXISTS last_digest TEXT COLLATE "C";
-- ---- END OMN-15376 shape reconciliation: pr_state ----
