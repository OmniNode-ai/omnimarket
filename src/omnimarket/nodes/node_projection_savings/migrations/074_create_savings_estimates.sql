-- OMN-10340: deterministic savings estimate projection table.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS savings_estimates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_timestamp TIMESTAMPTZ NOT NULL,
    session_id TEXT NOT NULL,
    model_local TEXT NOT NULL,
    model_cloud_baseline TEXT NOT NULL,
    local_cost_usd NUMERIC(18, 6) NOT NULL CHECK (local_cost_usd >= 0),
    cloud_cost_usd NUMERIC(18, 6) NOT NULL CHECK (cloud_cost_usd >= 0),
    savings_usd NUMERIC(18, 6) NOT NULL,
    repo_name TEXT,
    machine_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT savings_estimates_amounts_match
        CHECK (savings_usd = cloud_cost_usd - local_cost_usd)
);

-- ---- BEGIN OMN-15376 shape reconciliation: savings_estimates ----
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

ALTER TABLE savings_estimates ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE savings_estimates ADD COLUMN IF NOT EXISTS event_timestamp TIMESTAMPTZ;
ALTER TABLE savings_estimates ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE savings_estimates ADD COLUMN IF NOT EXISTS model_local TEXT;
ALTER TABLE savings_estimates ADD COLUMN IF NOT EXISTS model_cloud_baseline TEXT;
ALTER TABLE savings_estimates ADD COLUMN IF NOT EXISTS local_cost_usd NUMERIC(18, 6);
ALTER TABLE savings_estimates ADD COLUMN IF NOT EXISTS cloud_cost_usd NUMERIC(18, 6);
ALTER TABLE savings_estimates ADD COLUMN IF NOT EXISTS savings_usd NUMERIC(18, 6);
ALTER TABLE savings_estimates ADD COLUMN IF NOT EXISTS repo_name TEXT;
ALTER TABLE savings_estimates ADD COLUMN IF NOT EXISTS machine_id TEXT;
ALTER TABLE savings_estimates ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE savings_estimates ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'event_timestamp', 'session_id', 'model_local', 'model_cloud_baseline', 'local_cost_usd', 'cloud_cost_usd', 'savings_usd', 'created_at', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'savings_estimates'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'savings_estimates'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge savings_estimates.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'savings_estimates'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE savings_estimates ADD CONSTRAINT savings_estimates_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'savings_estimates'::regclass AND conname = 'savings_estimates_local_cost_usd_check'
    ) THEN
        ALTER TABLE savings_estimates ADD CONSTRAINT savings_estimates_local_cost_usd_check CHECK (local_cost_usd >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'savings_estimates'::regclass AND conname = 'savings_estimates_cloud_cost_usd_check'
    ) THEN
        ALTER TABLE savings_estimates ADD CONSTRAINT savings_estimates_cloud_cost_usd_check CHECK (cloud_cost_usd >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'savings_estimates'::regclass AND conname = 'savings_estimates_amounts_match'
    ) THEN
        ALTER TABLE savings_estimates ADD CONSTRAINT savings_estimates_amounts_match CHECK (savings_usd = cloud_cost_usd - local_cost_usd);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: savings_estimates ----


CREATE UNIQUE INDEX IF NOT EXISTS ux_savings_estimates_identity
    ON savings_estimates (
        session_id,
        event_timestamp,
        model_local,
        model_cloud_baseline
    );

CREATE OR REPLACE FUNCTION refresh_savings_estimates_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_savings_estimates_updated_at ON savings_estimates;
CREATE TRIGGER trg_savings_estimates_updated_at
    BEFORE UPDATE ON savings_estimates
    FOR EACH ROW
    EXECUTE FUNCTION refresh_savings_estimates_updated_at();
