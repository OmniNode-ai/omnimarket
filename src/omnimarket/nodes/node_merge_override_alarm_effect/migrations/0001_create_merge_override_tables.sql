-- OMN-23772: tables of node_merge_override_alarm_effect.
-- Receipts: the newest landing receipt (enforced arm or enqueue) per pull request.
CREATE TABLE IF NOT EXISTS omninode_internal.merge_override_receipts (
    repo_pr TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    operation TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    correlation_id TEXT NOT NULL
);

-- Pending: a merge whose head and merger are being read through the landing GitHub effect.
CREATE TABLE IF NOT EXISTS omninode_internal.merge_override_pending (
    repo_pr TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    correlation_id TEXT NOT NULL,
    source TEXT NOT NULL,
    merge_sha TEXT,
    requested_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_merge_override_pending_correlation
    ON omninode_internal.merge_override_pending (correlation_id);

-- Verdicts: one row per merge, every verdict kind.
CREATE TABLE IF NOT EXISTS omninode_internal.merge_override_verdicts (
    repo_pr TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    head_sha TEXT,
    merge_commit_sha TEXT,
    merger TEXT,
    source TEXT NOT NULL,
    receipt_seen BOOLEAN NOT NULL,
    ruling_row_id TEXT,
    reason TEXT,
    decided_at TIMESTAMPTZ NOT NULL
);

-- Open findings: merge_without_override and detector_unreadable, one row per finding key.
CREATE TABLE IF NOT EXISTS omninode_internal.merge_override_open_findings (
    finding_key TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT,
    verdict TEXT NOT NULL,
    reason TEXT,
    opened_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL
);
