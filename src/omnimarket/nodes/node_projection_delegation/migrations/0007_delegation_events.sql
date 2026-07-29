-- OMN-11765: base Postgres table for the delegation projection.
--
-- HandlerProjectionDelegation and DelegationProjectionRunner upsert into this
-- table using correlation_id as the deterministic idempotency key. Later
-- migrations add or backfill projection-specific metrics.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS delegation_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id TEXT NOT NULL UNIQUE,
    session_id TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    task_type TEXT NOT NULL DEFAULT '',
    delegated_to TEXT NOT NULL DEFAULT '',
    model_name TEXT NOT NULL DEFAULT '',
    delegated_by TEXT,
    quality_gate_passed BOOLEAN NOT NULL DEFAULT FALSE,
    quality_gates_checked INT NOT NULL DEFAULT 0,
    quality_gates_failed INT NOT NULL DEFAULT 0,
    quality_gates_checked_jsonb JSONB,
    quality_gates_failed_jsonb JSONB,
    quality_gate_detail TEXT,
    cost_usd NUMERIC NOT NULL DEFAULT 0,
    cost_savings_usd NUMERIC NOT NULL DEFAULT 0,
    delegation_latency_ms INT,
    latency_ms INT,
    repo TEXT,
    is_shadow BOOLEAN NOT NULL DEFAULT FALSE,
    llm_call_id TEXT,
    prompt_text TEXT,
    response_text TEXT,
    tokens_input INT NOT NULL DEFAULT 0,
    tokens_output INT NOT NULL DEFAULT 0,
    tokens_to_compliance INT NOT NULL DEFAULT 0,
    compliance_attempts INT NOT NULL DEFAULT 1,
    pricing_manifest_version INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: delegation_events ----
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

ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS timestamp TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS task_type TEXT DEFAULT '';
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS delegated_to TEXT DEFAULT '';
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS model_name TEXT DEFAULT '';
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS delegated_by TEXT;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS quality_gate_passed BOOLEAN DEFAULT FALSE;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS quality_gates_checked INT DEFAULT 0;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS quality_gates_failed INT DEFAULT 0;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS quality_gates_checked_jsonb JSONB;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS quality_gates_failed_jsonb JSONB;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS quality_gate_detail TEXT;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS cost_usd NUMERIC DEFAULT 0;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS cost_savings_usd NUMERIC DEFAULT 0;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS delegation_latency_ms INT;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS latency_ms INT;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS repo TEXT;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS is_shadow BOOLEAN DEFAULT FALSE;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS llm_call_id TEXT;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS prompt_text TEXT;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS response_text TEXT;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS tokens_input INT DEFAULT 0;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS tokens_output INT DEFAULT 0;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS tokens_to_compliance INT DEFAULT 0;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS compliance_attempts INT DEFAULT 1;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS pricing_manifest_version INT DEFAULT 0;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'correlation_id', 'timestamp', 'task_type', 'delegated_to', 'model_name', 'quality_gate_passed', 'quality_gates_checked', 'quality_gates_failed', 'cost_usd', 'cost_savings_usd', 'is_shadow', 'tokens_input', 'tokens_output', 'tokens_to_compliance', 'compliance_attempts', 'pricing_manifest_version', 'created_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'delegation_events'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'delegation_events'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge delegation_events.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'delegation_events'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE delegation_events ADD CONSTRAINT delegation_events_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'delegation_events'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['correlation_id']::text[]
    ) THEN
        ALTER TABLE delegation_events ADD CONSTRAINT delegation_events_correlation_id_key UNIQUE (correlation_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: delegation_events ----


-- Warm dev/stability volumes may already contain delegation_events from an
-- older projection schema. CREATE TABLE IF NOT EXISTS does not reconcile
-- missing columns, so keep this base migration idempotent before indexes and
-- later views reference the current shape.
ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS quality_gates_checked_jsonb JSONB,
    ADD COLUMN IF NOT EXISTS quality_gates_failed_jsonb JSONB,
    ADD COLUMN IF NOT EXISTS latency_ms INT,
    ADD COLUMN IF NOT EXISTS tokens_input INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS tokens_output INT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_delegation_events_timestamp
    ON delegation_events (timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_delegation_events_created_at
    ON delegation_events (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_delegation_events_task_type
    ON delegation_events (task_type);

CREATE INDEX IF NOT EXISTS idx_delegation_events_delegated_to
    ON delegation_events (delegated_to);
