-- OMN-12942: base table for the routing-decision dashboard projection view.
--
-- node_projection_llm_routing/0001_create_routing_dashboard_projection_view.sql
-- composes the `projection_routing_decision` view over `llm_routing_decisions`.
-- That base table is also created by the flat infra forward set
-- (omnibase_infra docker/migrations/forward/065_create_llm_routing_decisions.sql),
-- which the runner applies in its first phase. But this node-owned migration set
-- must be SELF-CONTAINED: the node forward-migration runner applies node dirs in
-- their own pass, and a clean apply of the node set alone (the path used by the
-- node-migration vendoring/sync flow) hard-fails the view migration when the flat
-- infra table is absent — exactly what happened during the 2026-06-11 04:24Z
-- stability hot-patch, which forced the view migration to be pruned. Owning the
-- base table inside the node removes that ordering dependency.
--
-- Schema is identical to the flat infra 065 table. All statements are
-- IF NOT EXISTS, so re-applying after 065 (or vice-versa) is a no-op — the two
-- migrations converge on the same table and are safe in either order.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS llm_routing_decisions (
    id                      UUID        NOT NULL DEFAULT gen_random_uuid(),
    correlation_id          UUID        NOT NULL,
    session_id              TEXT,

    -- Routing decision
    llm_agent               TEXT        NOT NULL,
    fuzzy_agent             TEXT,
    agreement               BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Confidence scores (0-1, NULL when not provided)
    llm_confidence          NUMERIC(5, 4) CHECK (llm_confidence IS NULL OR (llm_confidence >= 0 AND llm_confidence <= 1)),
    fuzzy_confidence        NUMERIC(5, 4) CHECK (fuzzy_confidence IS NULL OR (fuzzy_confidence >= 0 AND fuzzy_confidence <= 1)),

    -- Latency
    llm_latency_ms          INTEGER     NOT NULL DEFAULT 0,
    fuzzy_latency_ms        INTEGER     NOT NULL DEFAULT 0,

    -- Routing metadata
    used_fallback           BOOLEAN     NOT NULL DEFAULT FALSE,
    routing_prompt_version  TEXT        NOT NULL DEFAULT 'unknown',
    intent                  TEXT,
    model                   TEXT,
    cost_usd                NUMERIC(12, 8) CHECK (cost_usd IS NULL OR cost_usd >= 0),

    -- Audit
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projected_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_llm_routing_decisions PRIMARY KEY (id),
    CONSTRAINT uq_llm_routing_decisions_correlation UNIQUE (correlation_id)
);

CREATE INDEX IF NOT EXISTS idx_lrd_created_at
    ON llm_routing_decisions (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_lrd_agreement
    ON llm_routing_decisions (agreement, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_lrd_used_fallback
    ON llm_routing_decisions (used_fallback, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_lrd_prompt_version
    ON llm_routing_decisions (routing_prompt_version, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_lrd_agent_pair
    ON llm_routing_decisions (llm_agent, fuzzy_agent, created_at DESC)
    WHERE agreement = FALSE;

COMMENT ON TABLE llm_routing_decisions IS
    'LLM routing decision projection from onex.evt.omniclaude.llm-routing-decision.v1. '
    'UPSERT key: correlation_id (UUID). Base table for projection_routing_decision view (OMN-12942).';
