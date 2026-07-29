-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-13075: Baselines ROI snapshot projection table.
--
-- Backs the projection topic
--   onex.snapshot.projection.baselines.roi.v1
-- consumed by the omnidash BaselinesROICard widget (roi-trend, roi-by-model).
--
-- Source: per-snapshot ModelBaselinesComputedEvent carried on the topic
-- onex.evt.omnibase-infra.baselines-computed.v1.
-- One projection row per snapshot_id (upserted; latest per snapshot wins).
-- Fields:
--   token_delta   — sum of comparison.token_delta across all comparisons.
--   time_delta_ms — sum of comparison.time_delta_s * 1000 across all comparisons.
--   retry_delta   — sum of retry_count across all retry_counts.
--   recommendations — JSONB counting actions: promote / shadow / suppress / fork.
--   confidence    — average mapped confidence score across comparisons
--                   (high=1.0, medium=0.5, low=0.25; 0.0 when no comparisons).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS baselines_roi_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Identity — upsert conflict key
    snapshot_id TEXT NOT NULL,
    -- Source event timestamp
    captured_at TEXT NOT NULL,
    -- ROI aggregates
    token_delta BIGINT NOT NULL DEFAULT 0,
    time_delta_ms NUMERIC(18, 3) NOT NULL DEFAULT 0,
    retry_delta INTEGER NOT NULL DEFAULT 0 CHECK (retry_delta >= 0),
    -- Recommendation action counts stored as JSONB for structured access
    -- Shape: {"promote": N, "shadow": N, "suppress": N, "fork": N}
    recommendations JSONB NOT NULL DEFAULT '{"promote": 0, "shadow": 0, "suppress": 0, "fork": 0}',
    -- Average confidence score [0.0, 1.0]
    confidence NUMERIC(8, 6) NOT NULL DEFAULT 0 CHECK (confidence >= 0 AND confidence <= 1),
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
ALTER TABLE baselines_roi_snapshots ADD COLUMN IF NOT EXISTS time_delta_ms NUMERIC(18, 3) DEFAULT 0;
ALTER TABLE baselines_roi_snapshots ADD COLUMN IF NOT EXISTS retry_delta INTEGER DEFAULT 0;
ALTER TABLE baselines_roi_snapshots ADD COLUMN IF NOT EXISTS recommendations JSONB DEFAULT '{"promote": 0, "shadow": 0, "suppress": 0, "fork": 0}';
ALTER TABLE baselines_roi_snapshots ADD COLUMN IF NOT EXISTS confidence NUMERIC(8, 6) DEFAULT 0;
ALTER TABLE baselines_roi_snapshots ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'snapshot_id', 'captured_at', 'token_delta', 'time_delta_ms', 'retry_delta', 'recommendations', 'confidence', 'projected_at']
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
        WHERE conrelid = 'baselines_roi_snapshots'::regclass AND conname = 'baselines_roi_snapshots_retry_delta_check'
    ) THEN
        ALTER TABLE baselines_roi_snapshots ADD CONSTRAINT baselines_roi_snapshots_retry_delta_check CHECK (retry_delta >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baselines_roi_snapshots'::regclass AND conname = 'baselines_roi_snapshots_confidence_check'
    ) THEN
        ALTER TABLE baselines_roi_snapshots ADD CONSTRAINT baselines_roi_snapshots_confidence_check CHECK (confidence >= 0 AND confidence <= 1);
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
