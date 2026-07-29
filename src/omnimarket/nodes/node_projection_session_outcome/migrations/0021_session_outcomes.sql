-- OMN-10754: session_outcomes projection table.
-- Target DB: omnidash_analytics (omnibase_infra postgres on .201:5436)
-- Node: node_projection_session_outcome
-- UPSERT key: session_id (latest-state-wins)

CREATE TABLE IF NOT EXISTS session_outcomes (
    session_id  TEXT PRIMARY KEY,
    outcome     TEXT NOT NULL,
    emitted_at  TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: session_outcomes ----
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

ALTER TABLE session_outcomes ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE session_outcomes ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE session_outcomes ADD COLUMN IF NOT EXISTS emitted_at TIMESTAMPTZ;
ALTER TABLE session_outcomes ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE session_outcomes ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE session_outcomes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['session_id', 'outcome', 'emitted_at', 'ingested_at', 'created_at', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'session_outcomes'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'session_outcomes'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge session_outcomes.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'session_outcomes'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE session_outcomes ADD CONSTRAINT session_outcomes_pkey PRIMARY KEY (session_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: session_outcomes ----


CREATE INDEX IF NOT EXISTS idx_session_outcomes_emitted_at
    ON session_outcomes (emitted_at);

CREATE INDEX IF NOT EXISTS idx_session_outcomes_outcome
    ON session_outcomes (outcome);

CREATE OR REPLACE FUNCTION refresh_session_outcomes_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_session_outcomes_updated_at ON session_outcomes;
CREATE TRIGGER trg_session_outcomes_updated_at
    BEFORE UPDATE ON session_outcomes
    FOR EACH ROW
    EXECUTE FUNCTION refresh_session_outcomes_updated_at();
