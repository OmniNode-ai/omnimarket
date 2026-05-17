-- OMN-11184: project generation terminal events into generation_events table.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS generation_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id TEXT UNIQUE NOT NULL,
    task_description TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT '',
    endpoint_class TEXT NOT NULL DEFAULT '',
    attempt_count INT NOT NULL DEFAULT 0,
    total_latency_e2e_ms INT NOT NULL DEFAULT 0,
    contract_passed BOOLEAN NOT NULL DEFAULT FALSE,
    cost_inference_usd NUMERIC(18, 6) NOT NULL DEFAULT 0,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_generation_events_contract_passed
    ON generation_events (contract_passed);

CREATE INDEX IF NOT EXISTS idx_generation_events_timestamp
    ON generation_events (timestamp);
