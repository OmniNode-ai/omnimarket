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

-- ---- BEGIN OMN-15376 shape reconciliation: llm_delegation_daily_projection ----
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

ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS projection_date DATE;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS task_type TEXT;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS model_id TEXT;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS model_tier TEXT;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS total_calls INT DEFAULT 0;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS successful_calls INT DEFAULT 0;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS escalated_calls INT DEFAULT 0;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS total_tokens_in BIGINT DEFAULT 0;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS total_tokens_out BIGINT DEFAULT 0;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS total_latency_ms BIGINT DEFAULT 0;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS avg_latency_ms NUMERIC(12, 2) DEFAULT 0;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS total_actual_cost_usd NUMERIC(18, 8) DEFAULT 0;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS total_opus_equivalent_usd NUMERIC(18, 8) DEFAULT 0;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS total_savings_usd NUMERIC(18, 8) DEFAULT 0;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS avg_quality_score NUMERIC(6, 4);
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS projection_cursor TEXT;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS source_event_id TEXT;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS source_topic TEXT;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS source_partition INT;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS source_offset BIGINT;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS freshness_state TEXT DEFAULT 'FRESH';
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS reducer_version TEXT DEFAULT '1.0.0';
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE llm_delegation_daily_projection ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'projection_date', 'task_type', 'model_id', 'model_tier', 'total_calls', 'successful_calls', 'escalated_calls', 'total_tokens_in', 'total_tokens_out', 'total_latency_ms', 'avg_latency_ms', 'total_actual_cost_usd', 'total_opus_equivalent_usd', 'total_savings_usd', 'projection_cursor', 'source_event_id', 'source_topic', 'source_partition', 'source_offset', 'freshness_state', 'reducer_version', 'idempotency_key', 'observed_at', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'llm_delegation_daily_projection'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'llm_delegation_daily_projection'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge llm_delegation_daily_projection.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'llm_delegation_daily_projection'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE llm_delegation_daily_projection ADD CONSTRAINT llm_delegation_daily_projection_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'llm_delegation_daily_projection'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['model_id', 'model_tier', 'projection_date', 'task_type']::text[]
    ) THEN
        ALTER TABLE llm_delegation_daily_projection ADD CONSTRAINT uq_llm_delegation_daily_agg UNIQUE (projection_date, task_type, model_id, model_tier);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: llm_delegation_daily_projection ----


CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_delegation_idempotency_key
    ON llm_delegation_daily_projection (idempotency_key);

CREATE INDEX IF NOT EXISTS idx_llm_delegation_daily_date
    ON llm_delegation_daily_projection (projection_date DESC);

CREATE INDEX IF NOT EXISTS idx_llm_delegation_daily_model
    ON llm_delegation_daily_projection (model_id, model_tier);
