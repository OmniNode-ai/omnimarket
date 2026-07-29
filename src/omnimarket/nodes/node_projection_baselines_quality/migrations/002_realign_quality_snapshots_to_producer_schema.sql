-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-14630: realign baselines_quality_snapshots to the real producer schema.
--
-- WHY THIS EXISTS
--   001 shaped this table around a fictional local model
--   (patterns_compared/patterns_recommended/per-comparison string
--   confidence tiers) invented independently of the real producer contract
--   and sharing almost no field names with it. The real producer is
--   omnibase_infra.services.observability.baselines.models
--   .model_baselines_snapshot_event.ModelBaselinesSnapshotEvent, whose
--   breakdown rows (ModelBaselinesBreakdownRow) carry a numeric
--   sample-sufficiency confidence proxy (non-null only when
--   sample_count >= 20) and comparison rows (ModelBaselinesComparisonRow)
--   carry treatment_success_rate directly. See OMN-14513 for the sibling
--   node_projection_baselines fix and OMN-14630 for this node's own
--   field-overlap analysis.
--
--   Confirmed live on .201 stability-test (2026-07-14): the
--   projection_baselines_quality consumer group had committed offset 2
--   with zero lag (both real events on the topic consumed) yet
--   baselines_quality_snapshots held zero rows -- every real event silently
--   crashed inside the runtime's catch-all dispatch boundary since this
--   node was deployed.
--
-- Field changes:
--   patterns_compared        — redefined as len(event.breakdown); same name.
--   patterns_recommended     — renamed to patterns_significant (count of
--                               breakdown rows with confidence is not null,
--                               i.e. sample_count >= 20; the real event has
--                               no "recommendation" concept).
--   high/medium/low_confidence_count — redefined from breakdown.confidence
--                               thresholds instead of a fictional string
--                               tier (see handler docstring for thresholds).
--   quality_score             — redefined as mean(comparison
--                               .treatment_success_rate); real producer
--                               field.
--   recommend_rate            — renamed to significant_rate
--                               (patterns_significant / patterns_compared).
--
-- Recreated, not ALTERed: this table holds zero rows in every known
-- environment (verified live on .201 stability-test omnidash_analytics), so
-- there is no data to preserve across the shape change.
--
-- Idempotency: guarded with IF EXISTS / IF NOT EXISTS so this migration is
-- safe to re-apply.

DROP TABLE IF EXISTS baselines_quality_snapshots;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS baselines_quality_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Identity — upsert conflict key
    snapshot_id TEXT NOT NULL,
    -- Source event timestamp
    captured_at TEXT NOT NULL,
    -- Pattern counts from the snapshot's breakdown rows
    patterns_compared INTEGER NOT NULL DEFAULT 0 CHECK (patterns_compared >= 0),
    patterns_significant INTEGER NOT NULL DEFAULT 0 CHECK (patterns_significant >= 0),
    -- Confidence tier counts (derived from breakdown.confidence thresholds)
    high_confidence_count INTEGER NOT NULL DEFAULT 0 CHECK (high_confidence_count >= 0),
    medium_confidence_count INTEGER NOT NULL DEFAULT 0 CHECK (medium_confidence_count >= 0),
    low_confidence_count INTEGER NOT NULL DEFAULT 0 CHECK (low_confidence_count >= 0),
    -- Mean of non-null comparison.treatment_success_rate
    quality_score NUMERIC(8, 6) NOT NULL DEFAULT 0 CHECK (quality_score >= 0 AND quality_score <= 1),
    -- patterns_significant / max(1, patterns_compared)
    significant_rate NUMERIC(8, 6) NOT NULL DEFAULT 0 CHECK (significant_rate >= 0 AND significant_rate <= 1),
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
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS patterns_significant INTEGER DEFAULT 0;
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS high_confidence_count INTEGER DEFAULT 0;
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS medium_confidence_count INTEGER DEFAULT 0;
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS low_confidence_count INTEGER DEFAULT 0;
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS quality_score NUMERIC(8, 6) DEFAULT 0;
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS significant_rate NUMERIC(8, 6) DEFAULT 0;
ALTER TABLE baselines_quality_snapshots ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'snapshot_id', 'captured_at', 'patterns_compared', 'patterns_significant', 'high_confidence_count', 'medium_confidence_count', 'low_confidence_count', 'quality_score', 'significant_rate', 'projected_at']
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
        WHERE conrelid = 'baselines_quality_snapshots'::regclass AND conname = 'baselines_quality_snapshots_patterns_significant_check'
    ) THEN
        ALTER TABLE baselines_quality_snapshots ADD CONSTRAINT baselines_quality_snapshots_patterns_significant_check CHECK (patterns_significant >= 0);
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
        WHERE conrelid = 'baselines_quality_snapshots'::regclass AND conname = 'baselines_quality_snapshots_significant_rate_check'
    ) THEN
        ALTER TABLE baselines_quality_snapshots ADD CONSTRAINT baselines_quality_snapshots_significant_rate_check CHECK (significant_rate >= 0 AND significant_rate <= 1);
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
