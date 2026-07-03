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

-- Projection API orders by created_at DESC (most recent runs first).
CREATE INDEX IF NOT EXISTS idx_swarm_runs_created_at ON swarm_runs (created_at DESC);
