-- OMN-11184: project generation terminal events into generation_events table.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS generation_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id TEXT UNIQUE NOT NULL,
    task_description TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT '',
    endpoint_class TEXT NOT NULL DEFAULT '',
    attempt_count INT NOT NULL DEFAULT 0,
    total_latency_e2e_ms INT NOT NULL DEFAULT 0,
    contract_passed BOOLEAN NOT NULL DEFAULT FALSE,
    cost_inference_usd NUMERIC(18, 6) NOT NULL DEFAULT 0,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: generation_events ----
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

ALTER TABLE generation_events ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE generation_events ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE generation_events ADD COLUMN IF NOT EXISTS task_description TEXT DEFAULT '';
ALTER TABLE generation_events ADD COLUMN IF NOT EXISTS provider TEXT DEFAULT '';
ALTER TABLE generation_events ADD COLUMN IF NOT EXISTS model_id TEXT DEFAULT '';
ALTER TABLE generation_events ADD COLUMN IF NOT EXISTS endpoint_class TEXT DEFAULT '';
ALTER TABLE generation_events ADD COLUMN IF NOT EXISTS attempt_count INT DEFAULT 0;
ALTER TABLE generation_events ADD COLUMN IF NOT EXISTS total_latency_e2e_ms INT DEFAULT 0;
ALTER TABLE generation_events ADD COLUMN IF NOT EXISTS contract_passed BOOLEAN DEFAULT FALSE;
ALTER TABLE generation_events ADD COLUMN IF NOT EXISTS cost_inference_usd NUMERIC(18, 6) DEFAULT 0;
ALTER TABLE generation_events ADD COLUMN IF NOT EXISTS timestamp TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE generation_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'correlation_id', 'task_description', 'provider', 'model_id', 'endpoint_class', 'attempt_count', 'total_latency_e2e_ms', 'contract_passed', 'cost_inference_usd', 'timestamp', 'created_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'generation_events'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'generation_events'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge generation_events.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'generation_events'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE generation_events ADD CONSTRAINT generation_events_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'generation_events'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['correlation_id']::text[]
    ) THEN
        ALTER TABLE generation_events ADD CONSTRAINT generation_events_correlation_id_key UNIQUE (correlation_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: generation_events ----


CREATE INDEX IF NOT EXISTS idx_generation_events_contract_passed
    ON generation_events (contract_passed);

CREATE INDEX IF NOT EXISTS idx_generation_events_timestamp
    ON generation_events (timestamp);
