-- OMN-14648 / WS6: Create the merge_state_transitions projection table.
--
-- HandlerMergeStateProjection UPSERTs one row per
-- onex.evt.omnimarket.merge-state-transition.v1 event, deduped by event_id (the
-- deterministic 16-hex fingerprint of the transition's identifying tuple). The
-- merge-flow metrics (merge_state_metrics_native.compute_merge_flow_metrics) are
-- materialized from these rows: per-state duration, evidence-volume ratio
-- (baseline 1.67 -> target <=1.1), companions per product PR, same-head reruns
-- by reason code, queue wait, and product failures before vs after evidence.
--
-- projection_cursor is a strictly-monotonic BIGSERIAL: the generic projection
-- API filters rows with projection_cursor > :since and returns the largest
-- value as next_cursor, so a reader never re-processes a transition.
--
-- REPORT-ONLY: no enforcement / WIP cap is wired off this table in this PR.

CREATE TABLE IF NOT EXISTS merge_state_transitions (
    projection_cursor BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    branch TEXT NOT NULL DEFAULT '',
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    reason_code TEXT,
    is_occ_evidence BOOLEAN NOT NULL DEFAULT FALSE,
    product_pr_number INTEGER,
    queue_wait_seconds DOUBLE PRECISION,
    product_failure_found BOOLEAN NOT NULL DEFAULT FALSE,
    evidence_present BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_merge_state_transitions_cursor
    ON merge_state_transitions (projection_cursor);

CREATE INDEX IF NOT EXISTS idx_merge_state_transitions_repo_pr
    ON merge_state_transitions (repo, pr_number);

CREATE INDEX IF NOT EXISTS idx_merge_state_transitions_occurred_at
    ON merge_state_transitions (occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_merge_state_transitions_reason
    ON merge_state_transitions (reason_code);
