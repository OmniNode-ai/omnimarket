-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-13076: Baselines quality snapshot projection table.
--
-- Backs the projection topic
--   onex.snapshot.projection.baselines.quality.v1
-- consumed by the omnidash quality-baseline-panel widget.
--
-- Source: per-snapshot ModelBaselinesComputedEvent carried on the topic
-- onex.evt.omnibase-infra.baselines-computed.v1.
-- One projection row per snapshot_id (upserted; latest per snapshot wins).
-- Fields:
--   patterns_compared       — total patterns compared in the snapshot window.
--   patterns_recommended    — patterns for which a recommendation was produced.
--   high_confidence_count   — comparisons where confidence == "high".
--   medium_confidence_count — comparisons where confidence == "medium".
--   low_confidence_count    — comparisons where confidence == "low" (or unknown tier).
--   quality_score           — weighted avg: (high*1.0 + medium*0.5 + low*0.25)
--                             / max(1, total_comparisons).
--   recommend_rate          — patterns_recommended / max(1, patterns_compared).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS baselines_quality_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Identity — upsert conflict key
    snapshot_id TEXT NOT NULL,
    -- Source event timestamp
    captured_at TEXT NOT NULL,
    -- Pattern counts from the snapshot
    patterns_compared INTEGER NOT NULL DEFAULT 0 CHECK (patterns_compared >= 0),
    patterns_recommended INTEGER NOT NULL DEFAULT 0 CHECK (patterns_recommended >= 0),
    -- Confidence tier counts
    high_confidence_count INTEGER NOT NULL DEFAULT 0 CHECK (high_confidence_count >= 0),
    medium_confidence_count INTEGER NOT NULL DEFAULT 0 CHECK (medium_confidence_count >= 0),
    low_confidence_count INTEGER NOT NULL DEFAULT 0 CHECK (low_confidence_count >= 0),
    -- Derived quality metrics
    quality_score NUMERIC(8, 6) NOT NULL DEFAULT 0 CHECK (quality_score >= 0 AND quality_score <= 1),
    recommend_rate NUMERIC(8, 6) NOT NULL DEFAULT 0 CHECK (recommend_rate >= 0 AND recommend_rate <= 1),
    -- Projection metadata
    projected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: baselines_quality_snapshots ----
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

ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS snapshot_id TEXT;
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS captured_at TEXT;
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS patterns_compared INTEGER DEFAULT 0;
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS patterns_recommended INTEGER DEFAULT 0;
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS high_confidence_count INTEGER DEFAULT 0;
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS medium_confidence_count INTEGER DEFAULT 0;
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS low_confidence_count INTEGER DEFAULT 0;
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS quality_score NUMERIC(8, 6) DEFAULT 0;
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS recommend_rate NUMERIC(8, 6) DEFAULT 0;
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'snapshot_id', 'captured_at', 'patterns_compared', 'patterns_recommended', 'high_confidence_count', 'medium_confidence_count', 'low_confidence_count', 'quality_score', 'recommend_rate', 'projected_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'baselines_quality_snapshots'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'baselines_quality_snapshots'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge baselines_quality_snapshots.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baselines_quality_snapshots'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE baselines_quality_snapshots ADD CONSTRAINT baselines_quality_snapshots_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baselines_quality_snapshots'::regclass AND conname = 'baselines_quality_snapshots_patterns_compared_check'
    ) THEN
        ALTER TABLE baselines_quality_snapshots ADD CONSTRAINT baselines_quality_snapshots_patterns_compared_check CHECK (patterns_compared >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baselines_quality_snapshots'::regclass AND conname = 'baselines_quality_snapshots_patterns_recommended_check'
    ) THEN
        ALTER TABLE baselines_quality_snapshots ADD CONSTRAINT baselines_quality_snapshots_patterns_recommended_check CHECK (patterns_recommended >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baselines_quality_snapshots'::regclass AND conname = 'baselines_quality_snapshots_high_confidence_count_check'
    ) THEN
        ALTER TABLE baselines_quality_snapshots ADD CONSTRAINT baselines_quality_snapshots_high_confidence_count_check CHECK (high_confidence_count >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baselines_quality_snapshots'::regclass AND conname = 'baselines_quality_snapshots_medium_confidence_count_check'
    ) THEN
        ALTER TABLE baselines_quality_snapshots ADD CONSTRAINT baselines_quality_snapshots_medium_confidence_count_check CHECK (medium_confidence_count >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baselines_quality_snapshots'::regclass AND conname = 'baselines_quality_snapshots_low_confidence_count_check'
    ) THEN
        ALTER TABLE baselines_quality_snapshots ADD CONSTRAINT baselines_quality_snapshots_low_confidence_count_check CHECK (low_confidence_count >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baselines_quality_snapshots'::regclass AND conname = 'baselines_quality_snapshots_quality_score_check'
    ) THEN
        ALTER TABLE baselines_quality_snapshots ADD CONSTRAINT baselines_quality_snapshots_quality_score_check CHECK (quality_score >= 0 AND quality_score <= 1);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baselines_quality_snapshots'::regclass AND conname = 'baselines_quality_snapshots_recommend_rate_check'
    ) THEN
        ALTER TABLE baselines_quality_snapshots ADD CONSTRAINT baselines_quality_snapshots_recommend_rate_check CHECK (recommend_rate >= 0 AND recommend_rate <= 1);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: baselines_quality_snapshots ----


CREATE UNIQUE INDEX IF NOT EXISTS ux_baselines_quality_snapshots_snapshot_id
    ON baselines_quality_snapshots (snapshot_id);

CREATE INDEX IF NOT EXISTS ix_baselines_quality_snapshots_projected_at
    ON baselines_quality_snapshots (projected_at DESC);

CREATE OR REPLACE FUNCTION refresh_baselines_quality_snapshots_projected_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.projected_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_baselines_quality_snapshots_projected_at ON baselines_quality_snapshots;
CREATE TRIGGER trg_baselines_quality_snapshots_projected_at
    BEFORE UPDATE ON baselines_quality_snapshots
    FOR EACH ROW
    EXECUTE FUNCTION refresh_baselines_quality_snapshots_projected_at();
