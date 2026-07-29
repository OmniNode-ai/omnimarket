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

-- ---- BEGIN OMN-15376 shape reconciliation: capability_scores ----
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

ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS model_key TEXT;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS task_type TEXT;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS success_count INT DEFAULT 0;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS failure_count INT DEFAULT 0;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS total_count INT DEFAULT 0;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS success_rate DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS avg_latency_ms INT DEFAULT 0;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS avg_tokens_per_sec DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS total_cost DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS graduated BOOLEAN DEFAULT FALSE;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS last_updated TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'model_key', 'task_type', 'success_count', 'failure_count', 'total_count', 'success_rate', 'avg_latency_ms', 'avg_tokens_per_sec', 'total_cost', 'graduated', 'last_updated']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'public.capability_scores'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'public.capability_scores'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge public.capability_scores.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.capability_scores'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.capability_scores ADD CONSTRAINT capability_scores_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'public.capability_scores'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['model_key', 'task_type']::text[]
    ) THEN
        ALTER TABLE public.capability_scores ADD CONSTRAINT capability_scores_model_key_task_type_key UNIQUE (model_key, task_type);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: capability_scores ----


-- Lookup by model for router input
CREATE INDEX IF NOT EXISTS idx_capability_scores_model
    ON capability_scores (model_key);

-- Graduated models for fast filtering
CREATE INDEX IF NOT EXISTS idx_capability_scores_graduated
    ON capability_scores (graduated)
    WHERE graduated = TRUE;
