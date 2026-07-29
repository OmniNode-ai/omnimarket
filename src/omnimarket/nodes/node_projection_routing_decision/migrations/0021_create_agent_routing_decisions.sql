-- OMN-13150 (FIX-1): agent_routing_decisions projection table in omnidash_analytics.
-- Target DB: omnidash_analytics (omnibase_infra postgres on .201:5436)
-- Node: omnimarket.nodes.node_projection_routing_decision (DDL owner for the
--       omnidash_analytics copy of agent_routing_decisions).
-- Source topic: onex.evt.omniclaude.routing-decision.v1
-- Idempotency: INSERT ... ON CONFLICT (id) DO NOTHING (append-only observability).
--
-- WHY THIS MIGRATION EXISTS
-- The projection node's contract declares db_io.db_tables[0].database: omnidash_analytics,
-- and the runtime DB resolution (ModelProjectionRuntimeBinding.from_legacy_settings and the
-- projection-api _projection_database_binding_from_settings) both resolve
-- omnidash_analytics_db_url FIRST. The contract-declared AND runtime-resolved target is
-- therefore omnidash_analytics. But the base DDL
-- (omnibase_infra docker/migrations/forward/021_create_agent_routing_decisions_table.sql)
-- is applied by the flat infra forward set to the omnibase_infra DB only, so
-- agent_routing_decisions never landed in omnidash_analytics. All sibling projection tables
-- (session_outcomes, llm_call_metrics, pattern_learning_artifacts, llm_routing_decisions,
-- node_service_registry, savings_estimates, ...) live in omnidash_analytics via per-node
-- migrations under <node>/migrations/, vendored into
-- omnibase_infra/docker/migrations/forward/nodes/<node>/ and applied to $NODE_PGDB
-- (omnidash_analytics) by run-forward-migrations.sh. node_projection_routing_decision was
-- the only projection node missing its per-node migration; this file closes the gap.
--
-- This node-owned migration is SELF-CONTAINED and idempotent: every statement is
-- IF NOT EXISTS, so applying it to omnidash_analytics is independent of the flat infra 021
-- (which targets a different DB). Schema is identical to the flat infra 021 table
-- (id PK + the OMN-2057 project-context columns absorbed from omniclaude).

CREATE TABLE IF NOT EXISTS agent_routing_decisions (
    -- Identity
    id UUID PRIMARY KEY,
    correlation_id UUID,

    -- Routing decision
    selected_agent VARCHAR(255),
    confidence_score DECIMAL(5, 4),

    -- Audit (TTL keys off created_at)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Request context
    request_type VARCHAR(100),
    alternatives JSONB,
    routing_reason TEXT,
    domain VARCHAR(255),
    metadata JSONB,

    -- Project context (absorbed from omniclaude - OMN-2057)
    project_path TEXT,
    project_name VARCHAR(255),
    claude_session_id VARCHAR(255)
);

-- ---- BEGIN OMN-15376 shape reconciliation: agent_routing_decisions ----
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

ALTER TABLE agent_routing_decisions ADD COLUMN IF NOT EXISTS id UUID;
ALTER TABLE agent_routing_decisions ADD COLUMN IF NOT EXISTS correlation_id UUID;
ALTER TABLE agent_routing_decisions ADD COLUMN IF NOT EXISTS selected_agent VARCHAR(255);
ALTER TABLE agent_routing_decisions ADD COLUMN IF NOT EXISTS confidence_score DECIMAL(5, 4);
ALTER TABLE agent_routing_decisions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE agent_routing_decisions ADD COLUMN IF NOT EXISTS request_type VARCHAR(100);
ALTER TABLE agent_routing_decisions ADD COLUMN IF NOT EXISTS alternatives JSONB;
ALTER TABLE agent_routing_decisions ADD COLUMN IF NOT EXISTS routing_reason TEXT;
ALTER TABLE agent_routing_decisions ADD COLUMN IF NOT EXISTS domain VARCHAR(255);
ALTER TABLE agent_routing_decisions ADD COLUMN IF NOT EXISTS metadata JSONB;
ALTER TABLE agent_routing_decisions ADD COLUMN IF NOT EXISTS project_path TEXT;
ALTER TABLE agent_routing_decisions ADD COLUMN IF NOT EXISTS project_name VARCHAR(255);
ALTER TABLE agent_routing_decisions ADD COLUMN IF NOT EXISTS claude_session_id VARCHAR(255);

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'created_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'agent_routing_decisions'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'agent_routing_decisions'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge agent_routing_decisions.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'agent_routing_decisions'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE agent_routing_decisions ADD CONSTRAINT agent_routing_decisions_pkey PRIMARY KEY (id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: agent_routing_decisions ----


-- Minimal indexing for write-heavy workload - only TTL cleanup index.
CREATE INDEX IF NOT EXISTS idx_agent_routing_decisions_created_at
    ON agent_routing_decisions (created_at);

COMMENT ON TABLE agent_routing_decisions IS 'Agent routing decisions from polymorphic router (OMN-1743). Append-only observability projection (node_projection_routing_decision, omnidash_analytics).';
