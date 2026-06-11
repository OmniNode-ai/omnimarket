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
