-- OMN-12970: node-owned projection migration for llm_cost_aggregates.
--
-- WHY THIS EXISTS
--   node_projection_cost_summary declares projection_api over llm_cost_aggregates
--   (topic onex.snapshot.projection.cost.summary.v1). Like llm_call_metrics, this
--   table was historically created ONLY by omnibase_infra forward migration 031 in
--   the omnibase_infra DB, never in the dashboard projection DB
--   (omnidash_analytics) the projection API binds to. Result: the cost-summary
--   topic was DEGRADED at startup ("table 'public.llm_cost_aggregates' not found").
--
--   Vendored by omnibase_infra/scripts/sync-node-migrations.sh into
--   docker/migrations/forward/nodes/node_projection_cost_summary/ and applied to
--   NODE_POSTGRES_DB (omnidash_analytics) by run-forward-migrations.sh under the
--   namespaced migration id node:node_projection_cost_summary:<file>.
--
-- SCHEMA SOURCE OF TRUTH
--   Mirrors omnibase_infra forward migration
--   031_create_llm_call_metrics_and_cost_aggregates.sql (cost_aggregation_window
--   enum + llm_cost_aggregates table + indexes + updated_at trigger).
--
-- Idempotency: CREATE TYPE / TABLE / INDEX / TRIGGER guarded so the migration is
-- safe on a DB where the table already exists and on a fresh omnidash_analytics.

-- ============================================================================
-- ENUM: cost_aggregation_window
-- ============================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'cost_aggregation_window') THEN
        CREATE TYPE cost_aggregation_window AS ENUM ('24h', '7d', '30d');
    END IF;
END$$;

-- ============================================================================
-- LLM_COST_AGGREGATES TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS llm_cost_aggregates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    aggregation_key VARCHAR(512) NOT NULL,
    "window" cost_aggregation_window NOT NULL,

    total_cost_usd NUMERIC(14, 6) NOT NULL DEFAULT 0,
    total_tokens BIGINT NOT NULL DEFAULT 0,
    call_count INTEGER NOT NULL DEFAULT 0,

    estimated_coverage_pct NUMERIC(5, 2),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT unique_aggregation_key_window UNIQUE (aggregation_key, "window"),

    CONSTRAINT non_negative_total_cost_usd CHECK (total_cost_usd >= 0),
    CONSTRAINT non_negative_agg_total_tokens CHECK (total_tokens >= 0),
    CONSTRAINT non_negative_call_count CHECK (call_count >= 0),
    CONSTRAINT valid_estimated_coverage_pct CHECK (
        estimated_coverage_pct IS NULL
        OR (estimated_coverage_pct >= 0.00 AND estimated_coverage_pct <= 100.00)
    )
);

-- ---- BEGIN OMN-15376 shape reconciliation: llm_cost_aggregates ----
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

ALTER TABLE llm_cost_aggregates ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE llm_cost_aggregates ADD COLUMN IF NOT EXISTS aggregation_key VARCHAR(512);
ALTER TABLE llm_cost_aggregates ADD COLUMN IF NOT EXISTS "window" cost_aggregation_window;
ALTER TABLE llm_cost_aggregates ADD COLUMN IF NOT EXISTS total_cost_usd NUMERIC(14, 6) DEFAULT 0;
ALTER TABLE llm_cost_aggregates ADD COLUMN IF NOT EXISTS total_tokens BIGINT DEFAULT 0;
ALTER TABLE llm_cost_aggregates ADD COLUMN IF NOT EXISTS call_count INTEGER DEFAULT 0;
ALTER TABLE llm_cost_aggregates ADD COLUMN IF NOT EXISTS estimated_coverage_pct NUMERIC(5, 2);
ALTER TABLE llm_cost_aggregates ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE llm_cost_aggregates ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'aggregation_key', 'window', 'total_cost_usd', 'total_tokens', 'call_count', 'created_at', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'llm_cost_aggregates'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'llm_cost_aggregates'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge llm_cost_aggregates.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'llm_cost_aggregates'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE llm_cost_aggregates ADD CONSTRAINT llm_cost_aggregates_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'llm_cost_aggregates'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['aggregation_key', 'window']::text[]
    ) THEN
        ALTER TABLE llm_cost_aggregates ADD CONSTRAINT unique_aggregation_key_window UNIQUE (aggregation_key, "window");
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'llm_cost_aggregates'::regclass AND conname = 'non_negative_total_cost_usd'
    ) THEN
        ALTER TABLE llm_cost_aggregates ADD CONSTRAINT non_negative_total_cost_usd CHECK (total_cost_usd >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'llm_cost_aggregates'::regclass AND conname = 'non_negative_agg_total_tokens'
    ) THEN
        ALTER TABLE llm_cost_aggregates ADD CONSTRAINT non_negative_agg_total_tokens CHECK (total_tokens >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'llm_cost_aggregates'::regclass AND conname = 'non_negative_call_count'
    ) THEN
        ALTER TABLE llm_cost_aggregates ADD CONSTRAINT non_negative_call_count CHECK (call_count >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'llm_cost_aggregates'::regclass AND conname = 'valid_estimated_coverage_pct'
    ) THEN
        ALTER TABLE llm_cost_aggregates ADD CONSTRAINT valid_estimated_coverage_pct CHECK ( estimated_coverage_pct IS NULL OR (estimated_coverage_pct >= 0.00 AND estimated_coverage_pct <= 100.00) );
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: llm_cost_aggregates ----


CREATE INDEX IF NOT EXISTS idx_llm_cost_aggregates_aggregation_key
    ON llm_cost_aggregates (aggregation_key);

CREATE INDEX IF NOT EXISTS idx_llm_cost_aggregates_window
    ON llm_cost_aggregates ("window");

CREATE INDEX IF NOT EXISTS idx_llm_cost_aggregates_updated_at
    ON llm_cost_aggregates (updated_at DESC);

-- ============================================================================
-- TRIGGER: auto-update updated_at
-- ============================================================================
CREATE OR REPLACE FUNCTION update_llm_cost_aggregates_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_llm_cost_aggregates_updated_at ON llm_cost_aggregates;
CREATE TRIGGER trigger_llm_cost_aggregates_updated_at
    BEFORE UPDATE ON llm_cost_aggregates
    FOR EACH ROW
    EXECUTE FUNCTION update_llm_cost_aggregates_updated_at();
