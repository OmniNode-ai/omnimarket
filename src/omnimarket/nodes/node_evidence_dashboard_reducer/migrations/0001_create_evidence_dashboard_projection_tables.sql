-- OMN-11473: projection tables for evidence dashboard Waves 2/3.

CREATE TABLE IF NOT EXISTS evidence_dashboard_projection (
    projection_key TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    ticket_id TEXT,
    repo TEXT,
    pr_number INT,
    validation_run_id TEXT,
    current_stage TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    projection_cursor TEXT NOT NULL,
    last_event_id TEXT NOT NULL,
    last_ingest_sequence BIGINT,
    freshness_state TEXT NOT NULL,
    degraded_reason TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_correlation_trace_projection (
    event_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    ticket_id TEXT,
    repo TEXT,
    pr_number INT,
    source_topic TEXT NOT NULL,
    source_event_type TEXT NOT NULL,
    normalized_stage TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    ingest_sequence BIGINT,
    projection_cursor TEXT NOT NULL,
    last_event_id TEXT NOT NULL,
    last_ingest_sequence BIGINT,
    freshness_state TEXT NOT NULL,
    degraded_reason TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS evidence_readiness_aggregate_projection (
    aggregate_key TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    ticket_id TEXT,
    repo TEXT,
    pr_number INT,
    readiness_state TEXT NOT NULL,
    total_events INT NOT NULL DEFAULT 0,
    error_events INT NOT NULL DEFAULT 0,
    warning_events INT NOT NULL DEFAULT 0,
    projection_cursor TEXT NOT NULL,
    last_event_id TEXT NOT NULL,
    last_ingest_sequence BIGINT,
    freshness_state TEXT NOT NULL,
    degraded_reason TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_dashboard_projection_cursor
    ON evidence_dashboard_projection (projection_cursor);
CREATE INDEX IF NOT EXISTS idx_evidence_dashboard_observed_at
    ON evidence_dashboard_projection (observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_dashboard_expires_at
    ON evidence_dashboard_projection (expires_at);
CREATE INDEX IF NOT EXISTS idx_evidence_dashboard_ticket
    ON evidence_dashboard_projection (ticket_id);
CREATE INDEX IF NOT EXISTS idx_evidence_dashboard_repo_pr
    ON evidence_dashboard_projection (repo, pr_number);

CREATE INDEX IF NOT EXISTS idx_evidence_trace_correlation_sequence
    ON evidence_correlation_trace_projection (correlation_id, ingest_sequence, observed_at);
CREATE INDEX IF NOT EXISTS idx_evidence_trace_projection_cursor
    ON evidence_correlation_trace_projection (projection_cursor);
CREATE INDEX IF NOT EXISTS idx_evidence_trace_expires_at
    ON evidence_correlation_trace_projection (expires_at);
CREATE INDEX IF NOT EXISTS idx_evidence_trace_ticket
    ON evidence_correlation_trace_projection (ticket_id);

CREATE INDEX IF NOT EXISTS idx_evidence_readiness_projection_cursor
    ON evidence_readiness_aggregate_projection (projection_cursor);
CREATE INDEX IF NOT EXISTS idx_evidence_readiness_observed_at
    ON evidence_readiness_aggregate_projection (observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_readiness_expires_at
    ON evidence_readiness_aggregate_projection (expires_at);
CREATE INDEX IF NOT EXISTS idx_evidence_readiness_ticket
    ON evidence_readiness_aggregate_projection (ticket_id);
