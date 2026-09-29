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

-- ---- BEGIN OMN-15376 shape reconciliation: usage_by_model_day_calls ----
ALTER TABLE public.usage_by_model_day_calls ADD COLUMN IF NOT EXISTS call_id TEXT;
ALTER TABLE public.usage_by_model_day_calls ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE public.usage_by_model_day_calls ADD COLUMN IF NOT EXISTS usage_day DATE;
ALTER TABLE public.usage_by_model_day_calls ADD COLUMN IF NOT EXISTS model_id TEXT;
ALTER TABLE public.usage_by_model_day_calls ADD COLUMN IF NOT EXISTS input_tokens BIGINT;
ALTER TABLE public.usage_by_model_day_calls ADD COLUMN IF NOT EXISTS output_tokens BIGINT;
ALTER TABLE public.usage_by_model_day_calls ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(18,8);
ALTER TABLE public.usage_by_model_day_calls ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMPTZ;
ALTER TABLE public.usage_by_model_day_calls ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['call_id', 'tenant_id', 'usage_day', 'model_id', 'input_tokens', 'output_tokens', 'cost_usd', 'occurred_at', 'ingested_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL',
            'public.usage_by_model_day_calls'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL',
                'public.usage_by_model_day_calls'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge usage_by_model_day_calls.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.usage_by_model_day_calls'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.usage_by_model_day_calls
            ADD CONSTRAINT usage_by_model_day_calls_pkey PRIMARY KEY (call_id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.usage_by_model_day_calls'::regclass
          AND contype = 'c' AND conname = 'usage_by_model_day_calls_input_tokens_check'
    ) THEN
        ALTER TABLE public.usage_by_model_day_calls
            ADD CONSTRAINT usage_by_model_day_calls_input_tokens_check CHECK (input_tokens >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.usage_by_model_day_calls'::regclass
          AND contype = 'c' AND conname = 'usage_by_model_day_calls_output_tokens_check'
    ) THEN
        ALTER TABLE public.usage_by_model_day_calls
            ADD CONSTRAINT usage_by_model_day_calls_output_tokens_check CHECK (output_tokens >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.usage_by_model_day_calls'::regclass
          AND contype = 'c' AND conname = 'usage_by_model_day_calls_cost_usd_check'
    ) THEN
        ALTER TABLE public.usage_by_model_day_calls
            ADD CONSTRAINT usage_by_model_day_calls_cost_usd_check CHECK (cost_usd >= 0);
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: usage_by_model_day_calls ----

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

-- ---- BEGIN OMN-15376 shape reconciliation: usage_by_model_day ----
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS usage_day DATE;
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS model_id TEXT;
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS input_tokens BIGINT;
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS output_tokens BIGINT;
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(18,8);
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS call_count BIGINT;
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;

DO $$
DECLARE
    v_col TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['tenant_id', 'usage_day', 'model_id', 'input_tokens', 'output_tokens', 'cost_usd', 'call_count', 'updated_at', 'projection_cursor']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL',
            'public.usage_by_model_day'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL',
                'public.usage_by_model_day'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge usage_by_model_day.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.usage_by_model_day'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.usage_by_model_day
            ADD CONSTRAINT usage_by_model_day_pkey PRIMARY KEY (tenant_id, usage_day, model_id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.usage_by_model_day'::regclass
          AND contype = 'c' AND conname = 'usage_by_model_day_input_tokens_check'
    ) THEN
        ALTER TABLE public.usage_by_model_day
            ADD CONSTRAINT usage_by_model_day_input_tokens_check CHECK (input_tokens >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.usage_by_model_day'::regclass
          AND contype = 'c' AND conname = 'usage_by_model_day_output_tokens_check'
    ) THEN
        ALTER TABLE public.usage_by_model_day
            ADD CONSTRAINT usage_by_model_day_output_tokens_check CHECK (output_tokens >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.usage_by_model_day'::regclass
          AND contype = 'c' AND conname = 'usage_by_model_day_cost_usd_check'
    ) THEN
        ALTER TABLE public.usage_by_model_day
            ADD CONSTRAINT usage_by_model_day_cost_usd_check CHECK (cost_usd >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.usage_by_model_day'::regclass
          AND contype = 'c' AND conname = 'usage_by_model_day_call_count_check'
    ) THEN
        ALTER TABLE public.usage_by_model_day
            ADD CONSTRAINT usage_by_model_day_call_count_check CHECK (call_count >= 0);
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: usage_by_model_day ----

CREATE INDEX IF NOT EXISTS idx_usage_by_model_day_calls_key
    ON public.usage_by_model_day_calls (tenant_id, usage_day, model_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_by_model_day_projection_cursor
    ON public.usage_by_model_day (projection_cursor);
CREATE INDEX IF NOT EXISTS idx_usage_by_model_day_day_model
    ON public.usage_by_model_day (usage_day DESC, model_id ASC);
