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

-- Minimal indexing for write-heavy workload - only TTL cleanup index.
CREATE INDEX IF NOT EXISTS idx_agent_routing_decisions_created_at
    ON agent_routing_decisions (created_at);

COMMENT ON TABLE agent_routing_decisions IS 'Agent routing decisions from polymorphic router (OMN-1743). Append-only observability projection (node_projection_routing_decision, omnidash_analytics).';
