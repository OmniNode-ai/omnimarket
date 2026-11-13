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

-- ---- BEGIN OMN-15376 shape reconciliation: merge_override_receipts ----
-- Converges a drifted pre-existing table onto the declared shape; no-ops on fresh create. No DROP.
ALTER TABLE omninode_internal.merge_override_receipts ADD COLUMN IF NOT EXISTS repo_pr TEXT;
ALTER TABLE omninode_internal.merge_override_receipts ADD COLUMN IF NOT EXISTS repository TEXT;
ALTER TABLE omninode_internal.merge_override_receipts ADD COLUMN IF NOT EXISTS pr_number INTEGER;
ALTER TABLE omninode_internal.merge_override_receipts ADD COLUMN IF NOT EXISTS head_sha TEXT;
ALTER TABLE omninode_internal.merge_override_receipts ADD COLUMN IF NOT EXISTS operation TEXT;
ALTER TABLE omninode_internal.merge_override_receipts ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.merge_override_receipts ADD COLUMN IF NOT EXISTS correlation_id TEXT;
-- ---- END OMN-15376 shape reconciliation: merge_override_receipts ----

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

-- ---- BEGIN OMN-15376 shape reconciliation: merge_override_pending ----
-- Converges a drifted pre-existing table onto the declared shape; no-ops on fresh create. No DROP.
ALTER TABLE omninode_internal.merge_override_pending ADD COLUMN IF NOT EXISTS repo_pr TEXT;
ALTER TABLE omninode_internal.merge_override_pending ADD COLUMN IF NOT EXISTS repository TEXT;
ALTER TABLE omninode_internal.merge_override_pending ADD COLUMN IF NOT EXISTS pr_number INTEGER;
ALTER TABLE omninode_internal.merge_override_pending ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE omninode_internal.merge_override_pending ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE omninode_internal.merge_override_pending ADD COLUMN IF NOT EXISTS merge_sha TEXT;
ALTER TABLE omninode_internal.merge_override_pending ADD COLUMN IF NOT EXISTS requested_at TIMESTAMPTZ;
-- ---- END OMN-15376 shape reconciliation: merge_override_pending ----
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

-- ---- BEGIN OMN-15376 shape reconciliation: merge_override_verdicts ----
-- Converges a drifted pre-existing table onto the declared shape; no-ops on fresh create. No DROP.
ALTER TABLE omninode_internal.merge_override_verdicts ADD COLUMN IF NOT EXISTS repo_pr TEXT;
ALTER TABLE omninode_internal.merge_override_verdicts ADD COLUMN IF NOT EXISTS repository TEXT;
ALTER TABLE omninode_internal.merge_override_verdicts ADD COLUMN IF NOT EXISTS pr_number INTEGER;
ALTER TABLE omninode_internal.merge_override_verdicts ADD COLUMN IF NOT EXISTS verdict TEXT;
ALTER TABLE omninode_internal.merge_override_verdicts ADD COLUMN IF NOT EXISTS head_sha TEXT;
ALTER TABLE omninode_internal.merge_override_verdicts ADD COLUMN IF NOT EXISTS merge_commit_sha TEXT;
ALTER TABLE omninode_internal.merge_override_verdicts ADD COLUMN IF NOT EXISTS merger TEXT;
ALTER TABLE omninode_internal.merge_override_verdicts ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE omninode_internal.merge_override_verdicts ADD COLUMN IF NOT EXISTS receipt_seen BOOLEAN;
ALTER TABLE omninode_internal.merge_override_verdicts ADD COLUMN IF NOT EXISTS ruling_row_id TEXT;
ALTER TABLE omninode_internal.merge_override_verdicts ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE omninode_internal.merge_override_verdicts ADD COLUMN IF NOT EXISTS decided_at TIMESTAMPTZ;
-- ---- END OMN-15376 shape reconciliation: merge_override_verdicts ----

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

-- ---- BEGIN OMN-15376 shape reconciliation: merge_override_open_findings ----
-- Converges a drifted pre-existing table onto the declared shape; no-ops on fresh create. No DROP.
ALTER TABLE omninode_internal.merge_override_open_findings ADD COLUMN IF NOT EXISTS finding_key TEXT;
ALTER TABLE omninode_internal.merge_override_open_findings ADD COLUMN IF NOT EXISTS repository TEXT;
ALTER TABLE omninode_internal.merge_override_open_findings ADD COLUMN IF NOT EXISTS pr_number INTEGER;
ALTER TABLE omninode_internal.merge_override_open_findings ADD COLUMN IF NOT EXISTS head_sha TEXT;
ALTER TABLE omninode_internal.merge_override_open_findings ADD COLUMN IF NOT EXISTS verdict TEXT;
ALTER TABLE omninode_internal.merge_override_open_findings ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE omninode_internal.merge_override_open_findings ADD COLUMN IF NOT EXISTS opened_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.merge_override_open_findings ADD COLUMN IF NOT EXISTS source TEXT;
-- ---- END OMN-15376 shape reconciliation: merge_override_open_findings ----
