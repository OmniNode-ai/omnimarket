-- OMN-12970: node-owned projection migration for llm_call_metrics.
--
-- WHY THIS EXISTS
--   The projection API (omnimarket-*-projection-api) binds to the dashboard
--   projection database (omnidash_analytics), per
--   ModelProjectionDatabaseBinding precedence in
--   omnimarket/projection/api_server.py. Three projection contracts declare
--   llm_call_metrics as their read model:
--     - node_ab_compare_reducer        -> onex.snapshot.projection.ab-compare.v1
--     - node_projection_cost_token_usage -> onex.snapshot.projection.cost.token_usage.v1
--     - node_projection_llm_cost (writer/owner of this table)
--   The table was historically created ONLY by omnibase_infra forward
--   migration 031 in the omnibase_infra DB. It was never created in
--   omnidash_analytics, so the projection API marked the ab-compare topic
--   DEGRADED ("table 'public.llm_call_metrics' not found at startup") and the
--   dashboard panel rendered its empty state.
--
--   This node-owned migration is vendored by
--   omnibase_infra/scripts/sync-node-migrations.sh into
--   docker/migrations/forward/nodes/node_projection_llm_cost/ and applied to
--   NODE_POSTGRES_DB (omnidash_analytics) by run-forward-migrations.sh — the
--   same deploy-time materialization path every other marketplace projection
--   node uses. No renumber against the flat infra sequence is required: node
--   migrations are tracked under the namespaced id node:<node>:<file>.
--
-- SCHEMA SOURCE OF TRUTH
--   Mirrors omnibase_infra forward migration
--   031_create_llm_call_metrics_and_cost_aggregates.sql (table + enum + indexes)
--   so a row written in omnibase_infra is readable verbatim in omnidash_analytics
--   for the projection API. llm_cost_aggregates is intentionally NOT created
--   here: no projection contract declares it as a projection-API read model, so
--   it stays owned by the omnibase_infra DB.
--
-- Idempotency: CREATE TYPE / TABLE / INDEX guarded by IF NOT EXISTS so the
-- migration is safe on a DB where the table already exists (e.g. omnibase_infra)
-- and on a fresh omnidash_analytics.

-- ============================================================================
-- ENUM: usage_source_type
-- ============================================================================
-- Provenance of token usage data. Mirrors omnibase_infra migration 031.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'usage_source_type') THEN
        CREATE TYPE usage_source_type AS ENUM ('API', 'ESTIMATED', 'MISSING');
    END IF;
END$$;

-- ============================================================================
-- LLM_CALL_METRICS TABLE
-- ============================================================================
-- Per-LLM-call token usage and cost data. Append-only. Column set matches the
-- projection_api columns declared by node_ab_compare_reducer and
-- node_projection_cost_token_usage contracts, plus the provenance/idempotency
-- columns the node_projection_llm_cost consumer writes.
CREATE TABLE IF NOT EXISTS llm_call_metrics (
    -- Identity
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id UUID,

    -- Session/run context (logical references, no FK — async event arrival)
    session_id VARCHAR(255),
    run_id VARCHAR(255),

    -- Model identification
    model_id VARCHAR(255) NOT NULL,

    -- Token usage (nullable: not all providers report all fields)
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,

    -- Cost (NULL for unknown models, NOT 0)
    estimated_cost_usd NUMERIC(12, 6),

    -- Performance (nullable: failed LLM calls may not have latency data)
    latency_ms NUMERIC(10, 2),

    -- Usage provenance
    usage_source usage_source_type NOT NULL DEFAULT 'MISSING',
    usage_is_estimated BOOLEAN NOT NULL DEFAULT FALSE,

    -- Raw provider response (redacted, capped at 64KB)
    usage_raw JSONB,

    -- Provenance / idempotency columns
    input_hash VARCHAR(71),
    code_version VARCHAR(64),
    contract_version VARCHAR(64),
    source VARCHAR(255),

    -- Audit
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Constraints
    CONSTRAINT non_negative_prompt_tokens CHECK (
        prompt_tokens IS NULL OR prompt_tokens >= 0
    ),
    CONSTRAINT non_negative_completion_tokens CHECK (
        completion_tokens IS NULL OR completion_tokens >= 0
    ),
    CONSTRAINT non_negative_total_tokens CHECK (
        total_tokens IS NULL OR total_tokens >= 0
    ),
    CONSTRAINT non_negative_estimated_cost_usd CHECK (
        estimated_cost_usd IS NULL OR estimated_cost_usd >= 0
    ),
    CONSTRAINT non_negative_latency_ms CHECK (latency_ms IS NULL OR latency_ms >= 0),
    CONSTRAINT usage_raw_size_limit CHECK (
        usage_raw IS NULL OR octet_length(usage_raw::text) <= 65536
    )
);

-- ============================================================================
-- IDEMPOTENCY: input_hash unique (mirrors omnibase_infra migration 071)
-- ============================================================================
-- The node_projection_llm_cost consumer inserts with
-- ON CONFLICT (input_hash) DO NOTHING, which requires a unique constraint on
-- the (non-NULL) input_hash. A partial unique index keeps NULL input_hash rows
-- (legacy/unattributed writes) unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS ux_llm_call_metrics_input_hash
    ON llm_call_metrics (input_hash)
    WHERE input_hash IS NOT NULL;

-- ============================================================================
-- INDEXES
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_llm_call_metrics_session_id
    ON llm_call_metrics (session_id)
    WHERE session_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_llm_call_metrics_run_id
    ON llm_call_metrics (run_id)
    WHERE run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_llm_call_metrics_model_id
    ON llm_call_metrics (model_id);

CREATE INDEX IF NOT EXISTS idx_llm_call_metrics_correlation_id
    ON llm_call_metrics (correlation_id)
    WHERE correlation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_llm_call_metrics_created_at
    ON llm_call_metrics (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llm_call_metrics_model_created
    ON llm_call_metrics (model_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llm_call_metrics_usage_source
    ON llm_call_metrics (usage_source);
