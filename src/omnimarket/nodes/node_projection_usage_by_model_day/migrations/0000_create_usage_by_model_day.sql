-- OMN-19978: per-call ledger and tenant/model/UTC-day usage projection.
-- Owner: omnimarket.nodes.node_projection_usage_by_model_day
-- Target database: omnidash_analytics; physical schema: public.

CREATE TABLE IF NOT EXISTS public.usage_by_model_day_calls (
    call_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    usage_day DATE NOT NULL,
    model_id TEXT NOT NULL,
    input_tokens BIGINT NOT NULL CHECK (input_tokens >= 0),
    output_tokens BIGINT NOT NULL CHECK (output_tokens >= 0),
    cost_usd NUMERIC(18,8) NOT NULL CHECK (cost_usd >= 0),
    occurred_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.usage_by_model_day (
    tenant_id TEXT NOT NULL,
    usage_day DATE NOT NULL,
    model_id TEXT NOT NULL,
    input_tokens BIGINT NOT NULL CHECK (input_tokens >= 0),
    output_tokens BIGINT NOT NULL CHECK (output_tokens >= 0),
    cost_usd NUMERIC(18,8) NOT NULL CHECK (cost_usd >= 0),
    call_count BIGINT NOT NULL CHECK (call_count >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor BIGSERIAL NOT NULL,
    PRIMARY KEY (tenant_id, usage_day, model_id)
);

-- Reconcile columns on pre-existing tables, like the topic-activity migration.
ALTER TABLE public.usage_by_model_day_calls
    ADD COLUMN IF NOT EXISTS call_id TEXT;
ALTER TABLE public.usage_by_model_day_calls
    ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE public.usage_by_model_day_calls
    ADD COLUMN IF NOT EXISTS usage_day DATE;
ALTER TABLE public.usage_by_model_day_calls
    ADD COLUMN IF NOT EXISTS model_id TEXT;
ALTER TABLE public.usage_by_model_day_calls
    ADD COLUMN IF NOT EXISTS input_tokens BIGINT;
ALTER TABLE public.usage_by_model_day_calls
    ADD COLUMN IF NOT EXISTS output_tokens BIGINT;
ALTER TABLE public.usage_by_model_day_calls
    ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(18,8);
ALTER TABLE public.usage_by_model_day_calls
    ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMPTZ;
ALTER TABLE public.usage_by_model_day_calls
    ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE public.usage_by_model_day
    ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE public.usage_by_model_day
    ADD COLUMN IF NOT EXISTS usage_day DATE;
ALTER TABLE public.usage_by_model_day
    ADD COLUMN IF NOT EXISTS model_id TEXT;
ALTER TABLE public.usage_by_model_day
    ADD COLUMN IF NOT EXISTS input_tokens BIGINT;
ALTER TABLE public.usage_by_model_day
    ADD COLUMN IF NOT EXISTS output_tokens BIGINT;
ALTER TABLE public.usage_by_model_day
    ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(18,8);
ALTER TABLE public.usage_by_model_day
    ADD COLUMN IF NOT EXISTS call_count BIGINT;
ALTER TABLE public.usage_by_model_day
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE public.usage_by_model_day
    ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;

CREATE INDEX IF NOT EXISTS idx_usage_by_model_day_calls_key
    ON public.usage_by_model_day_calls (tenant_id, usage_day, model_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_by_model_day_projection_cursor
    ON public.usage_by_model_day (projection_cursor);
CREATE INDEX IF NOT EXISTS idx_usage_by_model_day_day_model
    ON public.usage_by_model_day (usage_day DESC, model_id ASC);
