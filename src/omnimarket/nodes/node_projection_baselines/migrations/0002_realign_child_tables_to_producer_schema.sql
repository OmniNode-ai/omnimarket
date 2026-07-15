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

CREATE INDEX IF NOT EXISTS idx_baselines_breakdown_snapshot
  ON baselines_breakdown (snapshot_id);
