-- Migration: 0000_create_gate_projection_tables.sql
-- Node: node_omnigate_projection
-- Ticket: OMN-13067
-- Creates gate_activity and gate_metrics tables for the OmniGate projection API.
-- These tables back the projection API endpoints for
-- onex.snapshot.projection.gate.activity.v1 and
-- onex.snapshot.projection.gate.metrics.v1.

CREATE TABLE IF NOT EXISTS gate_activity (
    id BIGSERIAL PRIMARY KEY,
    repository_id TEXT NOT NULL,
    project_name TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    base_sha TEXT NOT NULL DEFAULT '',
    head_sha TEXT NOT NULL DEFAULT '',
    diff_hash TEXT,
    config_hash TEXT,
    status TEXT NOT NULL,
    action TEXT,
    reason TEXT NOT NULL DEFAULT '',
    total_checks INTEGER NOT NULL DEFAULT 0,
    failed_checks INTEGER NOT NULL DEFAULT 0,
    advisory_checks INTEGER NOT NULL DEFAULT 0,
    pending_checks INTEGER NOT NULL DEFAULT 0,
    observed_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS gate_activity_observed_at_idx
    ON gate_activity (observed_at DESC);

CREATE INDEX IF NOT EXISTS gate_activity_repository_id_idx
    ON gate_activity (repository_id);

CREATE INDEX IF NOT EXISTS gate_activity_status_idx
    ON gate_activity (status);

-- Aggregate metrics snapshot — single row upserted on each event.
-- id=1 is the canonical singleton row.
CREATE TABLE IF NOT EXISTS gate_metrics (
    id INTEGER PRIMARY KEY DEFAULT 1,
    total_events INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    advisory INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed the singleton metrics row so the projection API returns 1 row immediately.
INSERT INTO gate_metrics (id, total_events, passed, failed, advisory, pending, updated_at)
VALUES (1, 0, 0, 0, 0, 0, NOW())
ON CONFLICT (id) DO NOTHING;
