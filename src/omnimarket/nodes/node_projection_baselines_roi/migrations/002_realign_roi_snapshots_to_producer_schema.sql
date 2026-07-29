-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-14630: realign baselines_roi_snapshots to the real producer schema.
--
-- WHY THIS EXISTS
--   001 shaped this table around a fictional per-pattern local model
--   (pattern_id/token_delta/time_delta_s/confidence/recommendations/
--   retry_counts) invented independently of the real producer contract and
--   sharing almost no field names with it. The real producer is
--   omnibase_infra.services.observability.baselines.models
--   .model_baselines_snapshot_event.ModelBaselinesSnapshotEvent, whose
--   comparison rows are ModelBaselinesComparisonRow (daily treatment-vs-
--   control shapes keyed by comparison_date, not pattern_id). See OMN-14513
--   for the sibling node_projection_baselines fix and OMN-14630 for this
--   node's own field-overlap analysis.
--
--   Confirmed live on .201 stability-test (2026-07-14): the
--   projection_baselines_roi consumer group had committed offset 2 with
--   zero lag (both real events on the topic consumed) yet
--   baselines_roi_snapshots held zero rows -- every real event silently
--   crashed inside the runtime's catch-all dispatch boundary since this
--   node was deployed.
--
-- New fields (no producer analog -> dropped, not remapped 1:1):
--   time_delta_ms, retry_delta, recommendations, confidence
-- Retained fields (redefined onto real producer-native fields):
--   token_delta = sum(control_total_tokens) - sum(treatment_total_tokens)
-- New fields (real producer-native aggregates):
--   roi_pct_avg, latency_improvement_pct_avg, cost_improvement_pct_avg,
--   sample_size
--
-- Recreated, not ALTERed: this table holds zero rows in every known
-- environment (verified live on .201 stability-test omnidash_analytics), so
-- there is no data to preserve across the shape change.
--
-- Idempotency: guarded with IF EXISTS / IF NOT EXISTS so this migration is
-- safe to re-apply.

DROP TABLE IF EXISTS baselines_roi_snapshots;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS baselines_roi_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Identity — upsert conflict key
    snapshot_id TEXT NOT NULL,
    -- Source event timestamp
    captured_at TEXT NOT NULL,
    -- Token savings: sum(control_total_tokens) - sum(treatment_total_tokens)
    -- across comparisons. Positive = treatment cohort used fewer tokens.
    token_delta BIGINT NOT NULL DEFAULT 0,
    -- Mean of non-null comparison.roi_pct
    roi_pct_avg DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- Mean of non-null comparison.latency_improvement_pct
    latency_improvement_pct_avg DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- Mean of non-null comparison.cost_improvement_pct
    cost_improvement_pct_avg DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- Sum of comparison.sample_size across all comparisons
    sample_size BIGINT NOT NULL DEFAULT 0 CHECK (sample_size >= 0),
    -- Projection metadata
    projected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: baselines_roi_snapshots ----
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

ALTER TABLE baselines_roi_snapshots ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE baselines_roi_snapshots ADD COLUMN IF NOT EXISTS snapshot_id TEXT;
ALTER TABLE baselines_roi_snapshots ADD COLUMN IF NOT EXISTS captured_at TEXT;
ALTER TABLE baselines_roi_snapshots ADD COLUMN IF NOT EXISTS token_delta BIGINT DEFAULT 0;
ALTER TABLE baselines_roi_snapshots ADD COLUMN IF NOT EXISTS roi_pct_avg DOUBLE PRECISION DEFAULT 0;
ALTER TABLE baselines_roi_snapshots ADD COLUMN IF NOT EXISTS latency_improvement_pct_avg DOUBLE PRECISION DEFAULT 0;
ALTER TABLE baselines_roi_snapshots ADD COLUMN IF NOT EXISTS cost_improvement_pct_avg DOUBLE PRECISION DEFAULT 0;
ALTER TABLE baselines_roi_snapshots ADD COLUMN IF NOT EXISTS sample_size BIGINT DEFAULT 0;
ALTER TABLE baselines_roi_snapshots ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'snapshot_id', 'captured_at', 'token_delta', 'roi_pct_avg', 'latency_improvement_pct_avg', 'cost_improvement_pct_avg', 'sample_size', 'projected_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'baselines_roi_snapshots'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'baselines_roi_snapshots'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge baselines_roi_snapshots.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baselines_roi_snapshots'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE baselines_roi_snapshots ADD CONSTRAINT baselines_roi_snapshots_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baselines_roi_snapshots'::regclass AND conname = 'baselines_roi_snapshots_sample_size_check'
    ) THEN
        ALTER TABLE baselines_roi_snapshots ADD CONSTRAINT baselines_roi_snapshots_sample_size_check CHECK (sample_size >= 0);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: baselines_roi_snapshots ----


CREATE UNIQUE INDEX IF NOT EXISTS ux_baselines_roi_snapshots_snapshot_id
    ON baselines_roi_snapshots (snapshot_id);

CREATE INDEX IF NOT EXISTS ix_baselines_roi_snapshots_projected_at
    ON baselines_roi_snapshots (projected_at DESC);

CREATE OR REPLACE FUNCTION refresh_baselines_roi_snapshots_projected_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.projected_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_baselines_roi_snapshots_projected_at ON baselines_roi_snapshots;
CREATE TRIGGER trg_baselines_roi_snapshots_projected_at
    BEFORE UPDATE ON baselines_roi_snapshots
    FOR EACH ROW
    EXECUTE FUNCTION refresh_baselines_roi_snapshots_projected_at();
