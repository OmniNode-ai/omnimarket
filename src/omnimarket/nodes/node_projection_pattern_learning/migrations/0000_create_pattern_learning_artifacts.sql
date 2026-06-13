-- Migration: 0000_create_pattern_learning_artifacts
-- Node: node_projection_pattern_learning
-- Target DB: omnidash_analytics (omnibase_infra postgres on .201:5436)
--
-- Purpose: Projection table for onex.evt.omniintelligence.pattern-stored.v1.
-- Consumed by node_projection_pattern_learning. Closes the golden chain
-- pattern_learning chain (OMN-13124, clears OMN-13102): the tail table had no
-- consumer for pattern-stored.v1, so the sweep timed out (0/N golden chains).
--
-- UPSERT key: pattern_id (latest-state-wins). Idempotent: IF NOT EXISTS.
-- Schema derived from omnibase_infra migration 064 + omnidash read-model, with
-- correlation_id added (the golden-chain expected field) and NOT-NULL defaults
-- relaxed so sparse pattern-stored events project without violating constraints.

CREATE TABLE IF NOT EXISTS pattern_learning_artifacts (
    id                  UUID         NOT NULL DEFAULT gen_random_uuid(),
    pattern_id          UUID         NOT NULL,
    pattern_name        VARCHAR(255) NOT NULL DEFAULT '',
    pattern_type        VARCHAR(100) NOT NULL DEFAULT '',
    language            VARCHAR(50),
    lifecycle_state     TEXT         NOT NULL DEFAULT 'candidate',
    state_changed_at    TIMESTAMPTZ,
    composite_score     NUMERIC(10, 6) NOT NULL DEFAULT 0,
    scoring_evidence    JSONB        NOT NULL DEFAULT '{}',
    signature           JSONB        NOT NULL DEFAULT '{}',
    metrics             JSONB        DEFAULT '{}',
    metadata            JSONB        DEFAULT '{}',
    correlation_id      TEXT,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    projected_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_pattern_learning_artifacts PRIMARY KEY (id),
    CONSTRAINT uq_pattern_learning_pattern_id UNIQUE (pattern_id)
);

-- Backfill correlation_id on pre-existing deployments of this table that were
-- created by the legacy omnibase_infra 064 migration (which lacked the column).
ALTER TABLE pattern_learning_artifacts
    ADD COLUMN IF NOT EXISTS correlation_id TEXT;

CREATE INDEX IF NOT EXISTS idx_patlearn_lifecycle_state
    ON pattern_learning_artifacts (lifecycle_state);

CREATE INDEX IF NOT EXISTS idx_patlearn_composite_score
    ON pattern_learning_artifacts (composite_score DESC);

CREATE INDEX IF NOT EXISTS idx_patlearn_state_changed_at
    ON pattern_learning_artifacts (state_changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_patlearn_created_at
    ON pattern_learning_artifacts (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_patlearn_updated_at
    ON pattern_learning_artifacts (updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_patlearn_pattern_name
    ON pattern_learning_artifacts (pattern_name);

COMMENT ON TABLE pattern_learning_artifacts IS
    'Pattern learning projection from onex.evt.omniintelligence.pattern-stored.v1. '
    'UPSERT key: pattern_id. Golden chain pattern_learning tail table (OMN-13124).';

COMMENT ON COLUMN pattern_learning_artifacts.pattern_id IS
    'Unique pattern identifier. Used as UPSERT conflict target.';

COMMENT ON COLUMN pattern_learning_artifacts.correlation_id IS
    'Correlation ID for distributed tracing. Golden-chain expected field.';
