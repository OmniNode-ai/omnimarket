-- OMN-12578 Phase 3: reducer-owned deployment evidence projection sink.
--
-- node_deployment_evidence_reducer materializes append-only evidence
-- validation events into these projection tables. The readiness gate consumes
-- this reducer-owned state (not logs or workflow summaries). Corrections and
-- supersessions are new events; prior rows are upserted by deployment_id, the
-- reducer's projection ordering authority is ingest_sequence.

CREATE TABLE IF NOT EXISTS deployment_evidence_projection (
    deployment_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    validation_run_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    evidence_lifecycle_state TEXT NOT NULL,
    validation_state TEXT NOT NULL,
    readiness_state TEXT NOT NULL,
    topology_affecting BOOLEAN NOT NULL DEFAULT FALSE,
    blocking_reason_codes TEXT NOT NULL DEFAULT '',
    contract_hash TEXT NOT NULL,
    evidence_bundle_hash TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS deployment_readiness_projection (
    deployment_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    validation_run_id TEXT NOT NULL,
    readiness_state TEXT NOT NULL,
    blocking_reason_codes TEXT NOT NULL DEFAULT '',
    gap_report_hash TEXT NOT NULL,
    validator_version TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_deployment_evidence_projection_ticket
    ON deployment_evidence_projection (ticket_id);
CREATE INDEX IF NOT EXISTS idx_deployment_evidence_projection_updated_at
    ON deployment_evidence_projection (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_deployment_evidence_projection_readiness
    ON deployment_evidence_projection (readiness_state);

CREATE INDEX IF NOT EXISTS idx_deployment_readiness_projection_updated_at
    ON deployment_readiness_projection (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_deployment_readiness_projection_readiness
    ON deployment_readiness_projection (readiness_state);
