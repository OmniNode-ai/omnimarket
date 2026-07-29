-- OMN-13084: node-owned projection migration for swarm_runs.
--
-- WHY THIS EXISTS
--   node_projection_swarm declares projection_api over swarm_runs (topic
--   onex.snapshot.projection.swarm.runs.v1). The projection API reads the
--   omnidash_analytics projection database; only node-owned migrations under
--   src/omnimarket/nodes/<node>/migrations/*.sql are vendored into that database
--   by omnibase_infra/scripts/sync-node-migrations.sh +
--   run-forward-migrations.sh (applied against NODE_POSTGRES_DB).
--
--   The original swarm_runs DDL (omnibase_infra forward/082_swarm_runs.sql) runs
--   against the flat infra database (POSTGRES_DB), not the projection database,
--   so the projection API would mark the topic DEGRADED ("table not found") and
--   the dashboard would render empty. This node-local migration materialises the
--   table in the projection database the API actually reads.
--
-- Idempotency: CREATE TABLE / INDEX guarded so the migration is safe to re-apply.
CREATE TABLE IF NOT EXISTS swarm_runs (
  run_id            TEXT PRIMARY KEY,
  correlation_id    TEXT NOT NULL,
  status            TEXT NOT NULL,
  task_hash         TEXT NOT NULL DEFAULT '',
  subtask_count     INTEGER NOT NULL DEFAULT 0,
  succeeded_count   INTEGER NOT NULL DEFAULT 0,
  failed_count      INTEGER NOT NULL DEFAULT 0,
  skipped_count     INTEGER NOT NULL DEFAULT 0,
  models_used       TEXT[] DEFAULT '{}',
  machines_used     TEXT[] DEFAULT '{}',
  total_cost_usd                DOUBLE PRECISION DEFAULT 0.0,
  cloud_equivalent_cost_usd     DOUBLE PRECISION DEFAULT 0.0,
  savings_usd                   DOUBLE PRECISION DEFAULT 0.0,
  parallelism_speedup_ratio     DOUBLE PRECISION DEFAULT 1.0,
  decomposition_latency_ms      INTEGER DEFAULT 0,
  dispatch_wall_latency_ms      INTEGER DEFAULT 0,
  aggregation_latency_ms        INTEGER DEFAULT 0,
  total_latency_ms              INTEGER DEFAULT 0,
  endpoint_registry_hash        TEXT DEFAULT '',
  registry_schema_version       TEXT DEFAULT '',
  projection_cursor             TEXT DEFAULT '',
  source_event_id               TEXT DEFAULT '',
  source_topic                  TEXT DEFAULT '',
  source_partition              INTEGER DEFAULT 0,
  source_offset                 INTEGER DEFAULT 0,
  reducer_version               TEXT DEFAULT '1.0.0',
  freshness_state               TEXT DEFAULT 'fresh',
  observed_at                   TIMESTAMPTZ,
  created_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: swarm_runs ----
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

ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS run_id TEXT;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS task_hash TEXT DEFAULT '';
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS subtask_count INTEGER DEFAULT 0;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS succeeded_count INTEGER DEFAULT 0;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS failed_count INTEGER DEFAULT 0;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS skipped_count INTEGER DEFAULT 0;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS models_used TEXT[] DEFAULT '{}';
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS machines_used TEXT[] DEFAULT '{}';
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS total_cost_usd DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS cloud_equivalent_cost_usd DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS savings_usd DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS parallelism_speedup_ratio DOUBLE PRECISION DEFAULT 1.0;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS decomposition_latency_ms INTEGER DEFAULT 0;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS dispatch_wall_latency_ms INTEGER DEFAULT 0;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS aggregation_latency_ms INTEGER DEFAULT 0;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS total_latency_ms INTEGER DEFAULT 0;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS endpoint_registry_hash TEXT DEFAULT '';
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS registry_schema_version TEXT DEFAULT '';
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS projection_cursor TEXT DEFAULT '';
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS source_event_id TEXT DEFAULT '';
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS source_topic TEXT DEFAULT '';
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS source_partition INTEGER DEFAULT 0;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS source_offset INTEGER DEFAULT 0;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS reducer_version TEXT DEFAULT '1.0.0';
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS freshness_state TEXT DEFAULT 'fresh';
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE swarm_runs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['run_id', 'correlation_id', 'status', 'task_hash', 'subtask_count', 'succeeded_count', 'failed_count', 'skipped_count', 'created_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'swarm_runs'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'swarm_runs'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge swarm_runs.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'swarm_runs'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE swarm_runs ADD CONSTRAINT swarm_runs_pkey PRIMARY KEY (run_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: swarm_runs ----


-- Projection API orders by created_at DESC (most recent runs first).
CREATE INDEX IF NOT EXISTS idx_swarm_runs_created_at ON swarm_runs (created_at DESC);
