-- OMN-13085: sandbox_decisions projection table.
-- Target DB: omnidash_analytics (omnibase_infra postgres on .201:5436)
-- Node: node_projection_sandbox_decisions
-- Append-only: conflict key = correlation_id (INSERT ... ON CONFLICT DO NOTHING)
-- Source: onex.evt.omnimarket.generated-node-invoked.v1

CREATE TABLE IF NOT EXISTS sandbox_decisions (
    correlation_id   TEXT PRIMARY KEY,
    node_name        TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    runtime_backend  TEXT NOT NULL DEFAULT 'sandbox'
                     CHECK (runtime_backend IN ('sandbox', 'runtime')),
    hot_load         BOOLEAN NOT NULL DEFAULT FALSE,
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: sandbox_decisions ----
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

ALTER TABLE sandbox_decisions ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE sandbox_decisions ADD COLUMN IF NOT EXISTS node_name TEXT;
ALTER TABLE sandbox_decisions ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE sandbox_decisions ADD COLUMN IF NOT EXISTS runtime_backend TEXT DEFAULT 'sandbox';
ALTER TABLE sandbox_decisions ADD COLUMN IF NOT EXISTS hot_load BOOLEAN DEFAULT FALSE;
ALTER TABLE sandbox_decisions ADD COLUMN IF NOT EXISTS error TEXT;
ALTER TABLE sandbox_decisions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['correlation_id', 'node_name', 'status', 'runtime_backend', 'hot_load', 'created_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'sandbox_decisions'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'sandbox_decisions'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge sandbox_decisions.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sandbox_decisions'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE sandbox_decisions ADD CONSTRAINT sandbox_decisions_pkey PRIMARY KEY (correlation_id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sandbox_decisions'::regclass AND conname = 'sandbox_decisions_status_check'
    ) THEN
        ALTER TABLE sandbox_decisions ADD CONSTRAINT sandbox_decisions_status_check CHECK (status IN ('completed', 'failed'));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sandbox_decisions'::regclass AND conname = 'sandbox_decisions_runtime_backend_check'
    ) THEN
        ALTER TABLE sandbox_decisions ADD CONSTRAINT sandbox_decisions_runtime_backend_check CHECK (runtime_backend IN ('sandbox', 'runtime'));
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: sandbox_decisions ----


CREATE INDEX IF NOT EXISTS idx_sandbox_decisions_created_at
    ON sandbox_decisions (created_at);

CREATE INDEX IF NOT EXISTS idx_sandbox_decisions_status
    ON sandbox_decisions (status);

CREATE INDEX IF NOT EXISTS idx_sandbox_decisions_node_name
    ON sandbox_decisions (node_name);
