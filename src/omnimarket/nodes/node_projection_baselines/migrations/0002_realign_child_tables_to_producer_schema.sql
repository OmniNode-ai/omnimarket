-- OMN-14513: realign baselines_comparisons / baselines_trend / baselines_breakdown
-- to the real producer row shapes.
--
-- WHY THIS EXISTS
--   0001 shaped these three tables to match BaselinesProjectionRunner
--   (handler_baselines.py), a handler whose field names (comparisons:
--   pattern_id/token_delta/...; trend: date/avg_cost_savings/...; breakdown:
--   action/count/avg_confidence) were invented independently of the real
--   producer contract and share almost no field names with it. The real
--   producer is
--   omnibase_infra.services.observability.baselines.models.model_baselines_snapshot_event
--   .ModelBaselinesSnapshotEvent, whose nested rows are
--   ModelBaselinesComparisonRow / ModelBaselinesTrendRow /
--   ModelBaselinesBreakdownRow (treatment-vs-control A/B shapes keyed by a
--   producer-issued row UUID, comparison_date/trend_date, and cohort /
--   pattern_id respectively). See OMN-14513 for the full field-overlap
--   analysis.
--
--   Confirmed live on .201 stability-test (2026-07-14): the
--   `projection_baselines` consumer group has committed offset 2 with zero
--   lag (both real events on the topic have been consumed) yet all four
--   tables hold zero rows -- every real event has silently crashed inside
--   the runtime's catch-all dispatch boundary since this node was deployed.
--   HandlerProjectionBaselines's old snapshot UPSERT wrote
--   patterns_compared/patterns_recommended, columns that do not exist on
--   baselines_snapshots, guaranteeing a DB error on every message.
--
-- Recreated, not ALTERed: all three tables hold zero rows in every known
-- environment (verified live on .201 stability-test omnidash_analytics), so
-- there is no data to preserve across the shape change. baselines_snapshots
-- is untouched -- its existing columns already match the producer's
-- top-level fields (snapshot_id, contract_version, computed_at_utc,
-- window_start_utc, window_end_utc); only the fictional
-- patterns_compared/patterns_recommended columns the old handler wrote are
-- dropped from the write path (in code, not schema -- 0001 never created
-- those columns on baselines_snapshots).
--
-- Idempotency: guarded with IF EXISTS / IF NOT EXISTS so this migration is
-- safe to re-apply.

-- =============================================================================
-- baselines_comparisons -- one row per ModelBaselinesComparisonRow. Primary
-- key is the producer's own row id (stable identity from the source-of-truth
-- infra table), not a local surrogate, so re-delivery of the same snapshot
-- upserts in place instead of duplicating.
-- =============================================================================
DROP TABLE IF EXISTS baselines_comparisons;

CREATE TABLE IF NOT EXISTS baselines_comparisons (
  id                         TEXT PRIMARY KEY,
  snapshot_id                TEXT NOT NULL,
  comparison_date            DATE NOT NULL,
  period_label               TEXT,
  treatment_sessions         BIGINT NOT NULL DEFAULT 0,
  treatment_success_rate     DOUBLE PRECISION,
  treatment_avg_latency_ms   DOUBLE PRECISION,
  treatment_avg_cost_tokens  DOUBLE PRECISION,
  treatment_total_tokens     BIGINT NOT NULL DEFAULT 0,
  control_sessions           BIGINT NOT NULL DEFAULT 0,
  control_success_rate       DOUBLE PRECISION,
  control_avg_latency_ms     DOUBLE PRECISION,
  control_avg_cost_tokens    DOUBLE PRECISION,
  control_total_tokens       BIGINT NOT NULL DEFAULT 0,
  roi_pct                    DOUBLE PRECISION,
  latency_improvement_pct    DOUBLE PRECISION,
  cost_improvement_pct       DOUBLE PRECISION,
  sample_size                BIGINT NOT NULL DEFAULT 0,
  computed_at                TIMESTAMPTZ NOT NULL,
  created_at                 TIMESTAMPTZ NOT NULL,
  updated_at                 TIMESTAMPTZ,
  projected_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: baselines_comparisons ----
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

ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS id TEXT;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS snapshot_id TEXT;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS comparison_date DATE;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS period_label TEXT;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS treatment_sessions BIGINT DEFAULT 0;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS treatment_success_rate DOUBLE PRECISION;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS treatment_avg_latency_ms DOUBLE PRECISION;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS treatment_avg_cost_tokens DOUBLE PRECISION;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS treatment_total_tokens BIGINT DEFAULT 0;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS control_sessions BIGINT DEFAULT 0;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS control_success_rate DOUBLE PRECISION;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS control_avg_latency_ms DOUBLE PRECISION;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS control_avg_cost_tokens DOUBLE PRECISION;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS control_total_tokens BIGINT DEFAULT 0;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS roi_pct DOUBLE PRECISION;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS latency_improvement_pct DOUBLE PRECISION;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS cost_improvement_pct DOUBLE PRECISION;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS sample_size BIGINT DEFAULT 0;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS computed_at TIMESTAMPTZ;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'snapshot_id', 'comparison_date', 'treatment_sessions', 'treatment_total_tokens', 'control_sessions', 'control_total_tokens', 'sample_size', 'computed_at', 'created_at', 'projected_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'baselines_comparisons'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'baselines_comparisons'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge baselines_comparisons.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baselines_comparisons'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE baselines_comparisons ADD CONSTRAINT baselines_comparisons_pkey PRIMARY KEY (id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: baselines_comparisons ----


CREATE INDEX IF NOT EXISTS idx_baselines_comparisons_snapshot
  ON baselines_comparisons (snapshot_id);

-- =============================================================================
-- baselines_trend -- one row per ModelBaselinesTrendRow: a single (cohort,
-- date) time-series data point. Primary key is the producer's row id.
-- =============================================================================
DROP TABLE IF EXISTS baselines_trend;

CREATE TABLE IF NOT EXISTS baselines_trend (
  id               TEXT PRIMARY KEY,
  snapshot_id      TEXT NOT NULL,
  trend_date       DATE NOT NULL,
  cohort           TEXT NOT NULL,
  session_count    BIGINT NOT NULL DEFAULT 0,
  success_rate     DOUBLE PRECISION,
  avg_latency_ms   DOUBLE PRECISION,
  avg_cost_tokens  DOUBLE PRECISION,
  roi_pct          DOUBLE PRECISION,
  computed_at      TIMESTAMPTZ NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL,
  projected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: baselines_trend ----
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

ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS id TEXT;
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS snapshot_id TEXT;
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS trend_date DATE;
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS cohort TEXT;
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS session_count BIGINT DEFAULT 0;
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS success_rate DOUBLE PRECISION;
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS avg_latency_ms DOUBLE PRECISION;
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS avg_cost_tokens DOUBLE PRECISION;
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS roi_pct DOUBLE PRECISION;
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS computed_at TIMESTAMPTZ;
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'snapshot_id', 'trend_date', 'cohort', 'session_count', 'computed_at', 'created_at', 'projected_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'baselines_trend'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'baselines_trend'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge baselines_trend.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baselines_trend'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE baselines_trend ADD CONSTRAINT baselines_trend_pkey PRIMARY KEY (id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: baselines_trend ----


CREATE INDEX IF NOT EXISTS idx_baselines_trend_snapshot
  ON baselines_trend (snapshot_id);

-- =============================================================================
-- baselines_breakdown -- one row per ModelBaselinesBreakdownRow: a single
-- pattern's treatment-vs-control performance. Primary key is the producer's
-- row id (distinct from pattern_id -- a pattern can recur across snapshots).
-- =============================================================================
DROP TABLE IF EXISTS baselines_breakdown;

CREATE TABLE IF NOT EXISTS baselines_breakdown (
  id                       TEXT PRIMARY KEY,
  snapshot_id              TEXT NOT NULL,
  pattern_id               TEXT NOT NULL,
  pattern_label            TEXT,
  treatment_success_rate   DOUBLE PRECISION,
  control_success_rate     DOUBLE PRECISION,
  roi_pct                  DOUBLE PRECISION,
  sample_count             BIGINT NOT NULL DEFAULT 0,
  treatment_count          BIGINT NOT NULL DEFAULT 0,
  control_count            BIGINT NOT NULL DEFAULT 0,
  confidence                DOUBLE PRECISION,
  computed_at              TIMESTAMPTZ NOT NULL,
  created_at               TIMESTAMPTZ NOT NULL,
  updated_at               TIMESTAMPTZ,
  projected_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: baselines_breakdown ----
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

ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS id TEXT;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS snapshot_id TEXT;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS pattern_id TEXT;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS pattern_label TEXT;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS treatment_success_rate DOUBLE PRECISION;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS control_success_rate DOUBLE PRECISION;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS roi_pct DOUBLE PRECISION;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS sample_count BIGINT DEFAULT 0;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS treatment_count BIGINT DEFAULT 0;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS control_count BIGINT DEFAULT 0;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS computed_at TIMESTAMPTZ;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'snapshot_id', 'pattern_id', 'sample_count', 'treatment_count', 'control_count', 'computed_at', 'created_at', 'projected_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'baselines_breakdown'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'baselines_breakdown'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge baselines_breakdown.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baselines_breakdown'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE baselines_breakdown ADD CONSTRAINT baselines_breakdown_pkey PRIMARY KEY (id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: baselines_breakdown ----


CREATE INDEX IF NOT EXISTS idx_baselines_breakdown_snapshot
  ON baselines_breakdown (snapshot_id);
