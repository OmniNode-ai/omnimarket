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

-- ---- BEGIN OMN-15376 shape reconciliation: baselines_snapshots ----
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

ALTER TABLE baselines_snapshots ADD COLUMN IF NOT EXISTS snapshot_id TEXT;
ALTER TABLE baselines_snapshots ADD COLUMN IF NOT EXISTS contract_version INTEGER DEFAULT 1;
ALTER TABLE baselines_snapshots ADD COLUMN IF NOT EXISTS computed_at_utc TIMESTAMPTZ;
ALTER TABLE baselines_snapshots ADD COLUMN IF NOT EXISTS window_start_utc TIMESTAMPTZ;
ALTER TABLE baselines_snapshots ADD COLUMN IF NOT EXISTS window_end_utc TIMESTAMPTZ;
ALTER TABLE baselines_snapshots ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['snapshot_id', 'contract_version', 'projected_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'baselines_snapshots'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'baselines_snapshots'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge baselines_snapshots.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'baselines_snapshots'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE baselines_snapshots ADD CONSTRAINT baselines_snapshots_pkey PRIMARY KEY (snapshot_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: baselines_snapshots ----


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

ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS snapshot_id TEXT;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS pattern_id TEXT;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS pattern_name TEXT DEFAULT '';
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS sample_size BIGINT DEFAULT 0;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS window_start TEXT DEFAULT '';
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS window_end TEXT DEFAULT '';
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS token_delta JSONB DEFAULT '{}'::jsonb;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS time_delta JSONB DEFAULT '{}'::jsonb;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS retry_delta JSONB DEFAULT '{}'::jsonb;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS test_pass_rate_delta JSONB DEFAULT '{}'::jsonb;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS review_iteration_delta JSONB DEFAULT '{}'::jsonb;
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS recommendation TEXT DEFAULT 'shadow';
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS confidence TEXT DEFAULT 'low';
ALTER TABLE baselines_comparisons ADD COLUMN IF NOT EXISTS rationale TEXT DEFAULT '';

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'snapshot_id', 'pattern_id', 'pattern_name', 'sample_size', 'window_start', 'window_end', 'token_delta', 'time_delta', 'retry_delta', 'test_pass_rate_delta', 'review_iteration_delta', 'recommendation', 'confidence', 'rationale']
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

ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS snapshot_id TEXT;
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS date TEXT;
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS avg_cost_savings TEXT DEFAULT '0';
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS avg_outcome_improvement TEXT DEFAULT '0';
ALTER TABLE baselines_trend ADD COLUMN IF NOT EXISTS comparisons_evaluated BIGINT DEFAULT 0;

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'snapshot_id', 'date', 'avg_cost_savings', 'avg_outcome_improvement', 'comparisons_evaluated']
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

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'baselines_trend'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['date', 'snapshot_id']::text[]
    ) THEN
        ALTER TABLE baselines_trend ADD CONSTRAINT uk_baselines_trend_snapshot_date UNIQUE (snapshot_id, date);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: baselines_trend ----


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

ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS snapshot_id TEXT;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS action TEXT;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS count BIGINT DEFAULT 0;
ALTER TABLE baselines_breakdown ADD COLUMN IF NOT EXISTS avg_confidence TEXT DEFAULT '0';

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'snapshot_id', 'action', 'count', 'avg_confidence']
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

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'baselines_breakdown'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['action', 'snapshot_id']::text[]
    ) THEN
        ALTER TABLE baselines_breakdown ADD CONSTRAINT uk_baselines_breakdown_snapshot_action UNIQUE (snapshot_id, action);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: baselines_breakdown ----


CREATE INDEX IF NOT EXISTS idx_baselines_breakdown_snapshot
  ON baselines_breakdown (snapshot_id);
