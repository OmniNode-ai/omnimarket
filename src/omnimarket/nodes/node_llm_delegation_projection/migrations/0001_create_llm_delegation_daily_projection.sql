-- OMN-11777: daily aggregate projection for LLM delegation calls.
-- Unique constraint drives UPSERT idempotency on (date, task_type, model_id, model_tier).
-- idempotency_key prevents replay duplication: correlation_id:causation_id:terminal_event_id.

CREATE TABLE IF NOT EXISTS llm_delegation_daily_projection (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Aggregate key — one row per (date, task_type, model_id, model_tier)
    projection_date             DATE NOT NULL,
    task_type                   TEXT NOT NULL,
    model_id                    TEXT NOT NULL,
    model_tier                  TEXT NOT NULL,

    -- Aggregate counters
    total_calls                 INT NOT NULL DEFAULT 0,
    successful_calls            INT NOT NULL DEFAULT 0,
    escalated_calls             INT NOT NULL DEFAULT 0,
    total_tokens_in             BIGINT NOT NULL DEFAULT 0,
    total_tokens_out            BIGINT NOT NULL DEFAULT 0,
    total_latency_ms            BIGINT NOT NULL DEFAULT 0,
    avg_latency_ms              NUMERIC(12, 2) NOT NULL DEFAULT 0,

    -- Cost aggregates (NUMERIC for monetary precision)
    total_actual_cost_usd       NUMERIC(18, 8) NOT NULL DEFAULT 0,
    total_opus_equivalent_usd   NUMERIC(18, 8) NOT NULL DEFAULT 0,
    total_savings_usd           NUMERIC(18, 8) NOT NULL DEFAULT 0,

    -- Quality
    avg_quality_score           NUMERIC(6, 4),

    -- Projection lineage
    projection_cursor           TEXT NOT NULL,          -- topic:partition:offset of last applied event
    source_event_id             TEXT NOT NULL,
    source_topic                TEXT NOT NULL,
    source_partition            INT NOT NULL,
    source_offset               BIGINT NOT NULL,
    freshness_state             TEXT NOT NULL DEFAULT 'FRESH',   -- FRESH | STALE | REPLAYING
    reducer_version             TEXT NOT NULL DEFAULT '1.0.0',

    -- Idempotency — prevents duplicate aggregate application on replay
    idempotency_key             TEXT NOT NULL,          -- correlation_id:causation_id:terminal_event_id

    observed_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_llm_delegation_daily_agg
        UNIQUE (projection_date, task_type, model_id, model_tier)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_delegation_idempotency_key
    ON llm_delegation_daily_projection (idempotency_key);

CREATE INDEX IF NOT EXISTS idx_llm_delegation_daily_date
    ON llm_delegation_daily_projection (projection_date DESC);

CREATE INDEX IF NOT EXISTS idx_llm_delegation_daily_model
    ON llm_delegation_daily_projection (model_id, model_tier);
