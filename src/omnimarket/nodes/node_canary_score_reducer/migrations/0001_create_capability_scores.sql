-- OMN-12970: node-owned projection migration for capability_scores.
--
-- WHY THIS EXISTS
--   node_canary_score_reducer declares projection_api over capability_scores
--   (topic onex.snapshot.projection.capability-scores.v1). The table was
--   historically created ONLY by omnibase_infra forward migration
--   060_create_routing_outcomes.sql in the omnibase_infra DB, never in the
--   dashboard projection DB (omnidash_analytics) the projection API binds to.
--   Result: the capability-scores topic was DEGRADED at startup
--   ("table 'public.capability_scores' not found").
--
--   Vendored by omnibase_infra/scripts/sync-node-migrations.sh into
--   docker/migrations/forward/nodes/node_canary_score_reducer/ and applied to
--   NODE_POSTGRES_DB (omnidash_analytics) by run-forward-migrations.sh under the
--   namespaced migration id node:node_canary_score_reducer:<file>.
--
-- SCHEMA SOURCE OF TRUTH
--   Mirrors the capability_scores table in omnibase_infra forward migration
--   060_create_routing_outcomes.sql (table + indexes). routing_outcomes is NOT
--   created here: no projection contract declares it as a projection-API read
--   model, so it stays owned by the omnibase_infra DB.
--
-- Idempotency: CREATE TABLE / INDEX guarded by IF NOT EXISTS so the migration is
-- safe on a DB where the table already exists and on a fresh omnidash_analytics.

-- ============================================================================
-- CAPABILITY_SCORES TABLE
-- ============================================================================
-- Persisted reducer state: rolling capability metrics per (model_key, task_type).
CREATE TABLE IF NOT EXISTS public.capability_scores (
    id                      BIGSERIAL        PRIMARY KEY,
    model_key               TEXT             NOT NULL,
    task_type               TEXT             NOT NULL,
    success_count           INT              NOT NULL DEFAULT 0,
    failure_count           INT              NOT NULL DEFAULT 0,
    total_count             INT              NOT NULL DEFAULT 0,
    success_rate            DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    avg_latency_ms          INT              NOT NULL DEFAULT 0,
    avg_tokens_per_sec      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    total_cost              DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    graduated               BOOLEAN          NOT NULL DEFAULT FALSE,
    last_updated            TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    UNIQUE (model_key, task_type)
);

-- Lookup by model for router input
CREATE INDEX IF NOT EXISTS idx_capability_scores_model
    ON capability_scores (model_key);

-- Graduated models for fast filtering
CREATE INDEX IF NOT EXISTS idx_capability_scores_graduated
    ON capability_scores (graduated)
    WHERE graduated = TRUE;
