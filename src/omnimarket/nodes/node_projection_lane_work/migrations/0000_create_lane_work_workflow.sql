-- =============================================================================
-- MIGRATION: one row per lane work workflow
-- =============================================================================
-- Ticket:  OMN-26708 (task 8 of the lane work orchestrator plan)
-- Owner:   omnimarket.nodes.node_projection_lane_work
-- Version: 1.0.0
--
-- Keyed by workflow id. Every column is derived from the workflow's transition
-- receipts and none has a default, so a row can never read as measured when it
-- was not: the writer supplies each value on insert. The terminal columns are
-- NULL while the workflow is live, which is a different fact from a value.
-- The upsert is guarded on seq, so a replay of the transition topic changes
-- nothing. omninode_internal is named explicitly, as the OMN-15361 gate requires.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.lane_work_workflow (
    workflow_id         TEXT             NOT NULL,
    state               TEXT             NOT NULL,
    last_event_kind     TEXT             NOT NULL,
    seq                 INTEGER          NOT NULL,
    started_at          TIMESTAMPTZ      NOT NULL,
    last_receipt_at     TIMESTAMPTZ      NOT NULL,
    transitions_taken   INTEGER          NOT NULL,
    help_request_count  INTEGER          NOT NULL,
    duration_seconds    DOUBLE PRECISION NOT NULL,
    terminal_kind       TEXT,
    ended_at            TIMESTAMPTZ,
    projected_at        TIMESTAMPTZ      NOT NULL,
    PRIMARY KEY (workflow_id)
);

-- The terminal rows: the orchestrator's view. Only an ended workflow is in it.
CREATE OR REPLACE VIEW omninode_internal.lane_work_terminal AS
SELECT workflow_id, state, last_event_kind, seq, started_at, last_receipt_at,
       transitions_taken, help_request_count, duration_seconds, terminal_kind,
       ended_at, projected_at
FROM omninode_internal.lane_work_workflow
WHERE terminal_kind IS NOT NULL;

-- ---- BEGIN OMN-15376 shape reconciliation: lane_work_workflow ----
ALTER TABLE omninode_internal.lane_work_workflow ADD COLUMN IF NOT EXISTS workflow_id TEXT;
ALTER TABLE omninode_internal.lane_work_workflow ADD COLUMN IF NOT EXISTS state TEXT;
ALTER TABLE omninode_internal.lane_work_workflow ADD COLUMN IF NOT EXISTS last_event_kind TEXT;
ALTER TABLE omninode_internal.lane_work_workflow ADD COLUMN IF NOT EXISTS seq INTEGER;
ALTER TABLE omninode_internal.lane_work_workflow ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.lane_work_workflow ADD COLUMN IF NOT EXISTS last_receipt_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.lane_work_workflow ADD COLUMN IF NOT EXISTS transitions_taken INTEGER;
ALTER TABLE omninode_internal.lane_work_workflow ADD COLUMN IF NOT EXISTS help_request_count INTEGER;
ALTER TABLE omninode_internal.lane_work_workflow ADD COLUMN IF NOT EXISTS duration_seconds DOUBLE PRECISION;
ALTER TABLE omninode_internal.lane_work_workflow ADD COLUMN IF NOT EXISTS terminal_kind TEXT;
ALTER TABLE omninode_internal.lane_work_workflow ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.lane_work_workflow ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ;
-- ---- END OMN-15376 shape reconciliation: lane_work_workflow ----
