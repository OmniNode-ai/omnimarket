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

-- ---- BEGIN OMN-15376 shape reconciliation: pattern_learning_artifacts ----
-- The CREATE TABLE IF NOT EXISTS above SILENTLY NO-OPS when a table of this
-- name already exists with a DIFFERENT shape (an out-of-band or legacy apply
-- that predates this migration). Everything below it in this file is NOT so
-- forgiving: CREATE INDEX IF NOT EXISTS guards the index NAME, not the COLUMN,
-- so the first column-dependent statement raises
--   ERROR: column "<col>" does not exist
-- and ON_ERROR_STOP=1 kills the whole migration Job there. Because the runner
-- halts at the first failure, instances of this class surface strictly one per
-- deploy cycle -- OMN-15376 (llm_cost_aggregates.aggregation_key, run
-- 30418878385) and OMN-15302 (baselines_comparisons.snapshot_id) each cost one.
--
-- The guarded adds below converge a drifted pre-existing table onto the shape
-- declared above. On the fresh-create path every one is a no-op (the column
-- already exists), so BOTH paths end at the same schema. No DROP, no recreate,
-- no TRUNCATE: pre-existing rows are preserved. A column that cannot be made
-- NOT NULL without inventing data fails LOUD and names the exact conflict
-- instead of guessing.
--
-- Gated by tests/ci/test_node_migration_shape_reconciliation.py (static) and
-- tests/integration/migrations/test_node_migration_shape_drift_omn15376.py
-- (RED/GREEN + fresh-vs-drifted schema equality on real Postgres).

ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS pattern_id UUID;
ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS pattern_name VARCHAR(255) DEFAULT '';
ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS pattern_type VARCHAR(100) DEFAULT '';
ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS language VARCHAR(50);
ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS lifecycle_state TEXT DEFAULT 'candidate';
ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS state_changed_at TIMESTAMPTZ;
ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS composite_score NUMERIC(10, 6) DEFAULT 0;
ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS scoring_evidence JSONB DEFAULT '{}';
ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS signature JSONB DEFAULT '{}';
ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS metrics JSONB DEFAULT '{}';
ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}';
ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE pattern_learning_artifacts ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'pattern_id', 'pattern_name', 'pattern_type', 'lifecycle_state', 'composite_score', 'scoring_evidence', 'signature', 'created_at', 'updated_at', 'projected_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'pattern_learning_artifacts'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'pattern_learning_artifacts'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge pattern_learning_artifacts.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'pattern_learning_artifacts'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE pattern_learning_artifacts ADD CONSTRAINT pk_pattern_learning_artifacts PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'pattern_learning_artifacts'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['pattern_id']::text[]
    ) THEN
        ALTER TABLE pattern_learning_artifacts ADD CONSTRAINT uq_pattern_learning_pattern_id UNIQUE (pattern_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: pattern_learning_artifacts ----


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
