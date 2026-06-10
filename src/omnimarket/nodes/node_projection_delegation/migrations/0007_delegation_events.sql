-- OMN-11765: base Postgres table for the delegation projection.
--
-- HandlerProjectionDelegation and DelegationProjectionRunner upsert into this
-- table using correlation_id as the deterministic idempotency key. Later
-- migrations add or backfill projection-specific metrics.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS delegation_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id TEXT NOT NULL UNIQUE,
    session_id TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    task_type TEXT NOT NULL DEFAULT '',
    delegated_to TEXT NOT NULL DEFAULT '',
    model_name TEXT NOT NULL DEFAULT '',
    delegated_by TEXT,
    quality_gate_passed BOOLEAN NOT NULL DEFAULT FALSE,
    quality_gates_checked INT NOT NULL DEFAULT 0,
    quality_gates_failed INT NOT NULL DEFAULT 0,
    quality_gates_checked_jsonb JSONB,
    quality_gates_failed_jsonb JSONB,
    quality_gate_detail TEXT,
    cost_usd NUMERIC NOT NULL DEFAULT 0,
    cost_savings_usd NUMERIC NOT NULL DEFAULT 0,
    delegation_latency_ms INT,
    latency_ms INT,
    repo TEXT,
    is_shadow BOOLEAN NOT NULL DEFAULT FALSE,
    llm_call_id TEXT,
    prompt_text TEXT,
    response_text TEXT,
    tokens_input INT NOT NULL DEFAULT 0,
    tokens_output INT NOT NULL DEFAULT 0,
    tokens_to_compliance INT NOT NULL DEFAULT 0,
    compliance_attempts INT NOT NULL DEFAULT 1,
    pricing_manifest_version INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Warm dev/stability volumes may already contain delegation_events from an
-- older projection schema. CREATE TABLE IF NOT EXISTS does not reconcile
-- missing columns, so keep this base migration idempotent before indexes and
-- later views reference the current shape.
ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS quality_gates_checked_jsonb JSONB,
    ADD COLUMN IF NOT EXISTS quality_gates_failed_jsonb JSONB,
    ADD COLUMN IF NOT EXISTS latency_ms INT,
    ADD COLUMN IF NOT EXISTS tokens_input INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS tokens_output INT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_delegation_events_timestamp
    ON delegation_events (timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_delegation_events_created_at
    ON delegation_events (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_delegation_events_task_type
    ON delegation_events (task_type);

CREATE INDEX IF NOT EXISTS idx_delegation_events_delegated_to
    ON delegation_events (delegated_to);
