-- OMN-11887: node-owned projection migration for the baselines read model.
--
-- WHY THIS EXISTS
--   node_projection_baselines declares four projection tables under
--   db_io.db_tables — baselines_snapshots, baselines_comparisons,
--   baselines_trend, baselines_breakdown — in database omnidash_analytics.
--   Every row previously pointed at "0001_omnidash_analytics_read_model.sql",
--   a file that exists in no repo, and the node shipped no migrations/
--   directory. The omnimarket projection migration runner
--   (scripts/run-projection-migrations.py) discovers node-owned migrations by
--   globbing src/omnimarket/nodes/<node>/migrations/*.sql and applies them
--   against the projection database (omnidash_analytics). With no file, the
--   four tables were never created and the projection could never materialize.
--
--   This is the same node-owned-projection-migration pattern used by
--   node_projection_swarm/migrations/0001_create_swarm_runs.sql and
--   node_projection_overnight/migrations/. The infra-side baselines DDL
--   (omnibase_infra/docker/migrations/forward/050_create_baselines_tables.sql)
--   targets the flat infra database with a different A/B-cohort schema; it does
--   NOT create the snapshot-keyed read model that this projection writes, so
--   this node-owned migration materializes the tables the projection API reads.
--
-- SCHEMA SOURCE OF TRUTH
--   Column shapes mirror BaselinesProjectionRunner (handler_baselines.py), the
--   handler whose contract db_tables roles (snapshots/comparisons/trend/
--   breakdown) these tables back. It writes each snapshot + its child rows
--   transactionally via raw asyncpg INSERTs with no server-side casts beyond
--   the explicit ::jsonb on the comparison delta columns. Values the handler
--   serializes to text before insert (trend dates / savings, breakdown
--   confidence) are stored as TEXT so the runtime insert succeeds without a
--   handler-side type change.
--
-- Idempotency: every CREATE TABLE / CREATE INDEX is guarded with IF NOT EXISTS,
-- so this migration is safe to re-apply over an already-seeded database.

-- =============================================================================
-- baselines_snapshots — one parent row per computed baselines snapshot.
-- UPSERT target: ON CONFLICT (snapshot_id) in the projection handler.
-- =============================================================================
CREATE TABLE IF NOT EXISTS baselines_snapshots (
  snapshot_id       TEXT PRIMARY KEY,
  contract_version  INTEGER NOT NULL DEFAULT 1,
  computed_at_utc   TIMESTAMPTZ,
  window_start_utc  TIMESTAMPTZ,
  window_end_utc    TIMESTAMPTZ,
  projected_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_baselines_snapshots_computed_at
  ON baselines_snapshots (computed_at_utc DESC);

-- =============================================================================
-- baselines_comparisons — per-pattern comparison rows for a snapshot.
-- The handler DELETEs by snapshot_id then re-INSERTs, so rows are not unique
-- per pattern_id; a surrogate id is the primary key.
-- =============================================================================
CREATE TABLE IF NOT EXISTS baselines_comparisons (
  id                      BIGSERIAL PRIMARY KEY,
  snapshot_id             TEXT NOT NULL,
  pattern_id              TEXT NOT NULL,
  pattern_name            TEXT NOT NULL DEFAULT '',
  sample_size             BIGINT NOT NULL DEFAULT 0,
  window_start            TEXT NOT NULL DEFAULT '',
  window_end              TEXT NOT NULL DEFAULT '',
  token_delta             JSONB NOT NULL DEFAULT '{}'::jsonb,
  time_delta              JSONB NOT NULL DEFAULT '{}'::jsonb,
  retry_delta             JSONB NOT NULL DEFAULT '{}'::jsonb,
  test_pass_rate_delta    JSONB NOT NULL DEFAULT '{}'::jsonb,
  review_iteration_delta  JSONB NOT NULL DEFAULT '{}'::jsonb,
  recommendation          TEXT NOT NULL DEFAULT 'shadow',
  confidence              TEXT NOT NULL DEFAULT 'low',
  rationale               TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_baselines_comparisons_snapshot
  ON baselines_comparisons (snapshot_id);

-- =============================================================================
-- baselines_trend — per-day trend rows for a snapshot. The handler dedups by
-- date before insert; the (snapshot_id, date) pair is unique. avg_* values are
-- handler-serialized to text before insert (see SCHEMA SOURCE OF TRUTH above).
-- =============================================================================
CREATE TABLE IF NOT EXISTS baselines_trend (
  id                       BIGSERIAL PRIMARY KEY,
  snapshot_id              TEXT NOT NULL,
  date                     TEXT NOT NULL,
  avg_cost_savings         TEXT NOT NULL DEFAULT '0',
  avg_outcome_improvement  TEXT NOT NULL DEFAULT '0',
  comparisons_evaluated    BIGINT NOT NULL DEFAULT 0,
  CONSTRAINT uk_baselines_trend_snapshot_date UNIQUE (snapshot_id, date)
);

CREATE INDEX IF NOT EXISTS idx_baselines_trend_snapshot
  ON baselines_trend (snapshot_id);

-- =============================================================================
-- baselines_breakdown — per-action breakdown rows for a snapshot. The handler
-- dedups by action before insert; the (snapshot_id, action) pair is unique.
-- avg_confidence is handler-serialized to text before insert.
-- =============================================================================
CREATE TABLE IF NOT EXISTS baselines_breakdown (
  id              BIGSERIAL PRIMARY KEY,
  snapshot_id     TEXT NOT NULL,
  action          TEXT NOT NULL,
  count           BIGINT NOT NULL DEFAULT 0,
  avg_confidence  TEXT NOT NULL DEFAULT '0',
  CONSTRAINT uk_baselines_breakdown_snapshot_action UNIQUE (snapshot_id, action)
);

CREATE INDEX IF NOT EXISTS idx_baselines_breakdown_snapshot
  ON baselines_breakdown (snapshot_id);
