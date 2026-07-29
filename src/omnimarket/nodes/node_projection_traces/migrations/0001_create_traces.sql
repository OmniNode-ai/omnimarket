-- OMN-13083: node-owned projection migration for traces.
--
-- WHY THIS EXISTS
--   node_projection_traces declares projection_api over the traces table
--   (topic onex.snapshot.projection.traces.v1). The dashboard trace-explorer
--   widget's projectionSchema requires the correlation-grouped row shape
--   (correlation_id, nodes_involved, event_count, first_event_at,
--   last_event_at, duration_ms, has_error, is_running, latest_message).
--   No contract previously backed that topic; OMN-12135 wired it to a bespoke
--   Express query and OMN-12822 removed that bespoke server. This node-owned
--   table is the canonical backing surface for the projection API.
--
--   Discovered + applied by scripts/run-projection-migrations.py (node-owned
--   migrations/ discovery) and vendored to the dashboard projection DB
--   (omnidash_analytics) the projection API binds to.
--
-- Idempotency: CREATE TABLE / INDEX / TRIGGER guarded so the migration is safe
-- on a DB where the table already exists and on a fresh omnidash_analytics.

-- ============================================================================
-- TRACES TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS traces (
    correlation_id VARCHAR(256) PRIMARY KEY,

    nodes_involved TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    event_count INTEGER NOT NULL DEFAULT 0,

    first_event_at TIMESTAMPTZ NOT NULL,
    last_event_at TIMESTAMPTZ NOT NULL,
    duration_ms BIGINT NOT NULL DEFAULT 0,

    has_error BOOLEAN NOT NULL DEFAULT FALSE,
    is_running BOOLEAN NOT NULL DEFAULT TRUE,
    latest_message TEXT NOT NULL DEFAULT '',

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT non_negative_traces_event_count CHECK (event_count >= 0),
    CONSTRAINT non_negative_traces_duration_ms CHECK (duration_ms >= 0)
);

-- ---- BEGIN OMN-15376 shape reconciliation: traces ----
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

ALTER TABLE traces ADD COLUMN IF NOT EXISTS correlation_id VARCHAR(256);
ALTER TABLE traces ADD COLUMN IF NOT EXISTS nodes_involved TEXT[] DEFAULT ARRAY[]::TEXT[];
ALTER TABLE traces ADD COLUMN IF NOT EXISTS event_count INTEGER DEFAULT 0;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS first_event_at TIMESTAMPTZ;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS last_event_at TIMESTAMPTZ;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS duration_ms BIGINT DEFAULT 0;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS has_error BOOLEAN DEFAULT FALSE;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS is_running BOOLEAN DEFAULT TRUE;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS latest_message TEXT DEFAULT '';
ALTER TABLE traces ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE traces ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['correlation_id', 'nodes_involved', 'event_count', 'first_event_at', 'last_event_at', 'duration_ms', 'has_error', 'is_running', 'latest_message', 'created_at', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'traces'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'traces'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge traces.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'traces'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE traces ADD CONSTRAINT traces_pkey PRIMARY KEY (correlation_id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'traces'::regclass AND conname = 'non_negative_traces_event_count'
    ) THEN
        ALTER TABLE traces ADD CONSTRAINT non_negative_traces_event_count CHECK (event_count >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'traces'::regclass AND conname = 'non_negative_traces_duration_ms'
    ) THEN
        ALTER TABLE traces ADD CONSTRAINT non_negative_traces_duration_ms CHECK (duration_ms >= 0);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: traces ----


CREATE INDEX IF NOT EXISTS idx_traces_last_event_at
    ON traces (last_event_at DESC);

CREATE INDEX IF NOT EXISTS idx_traces_has_error
    ON traces (has_error);

CREATE INDEX IF NOT EXISTS idx_traces_is_running
    ON traces (is_running);

-- ============================================================================
-- TRIGGER: auto-update updated_at
-- ============================================================================
CREATE OR REPLACE FUNCTION update_traces_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_traces_updated_at ON traces;
CREATE TRIGGER trigger_traces_updated_at
    BEFORE UPDATE ON traces
    FOR EACH ROW
    EXECUTE FUNCTION update_traces_updated_at();
