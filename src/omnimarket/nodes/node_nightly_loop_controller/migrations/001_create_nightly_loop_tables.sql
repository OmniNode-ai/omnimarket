-- Migration: Create nightly loop controller tables
-- Target DB: omnidash_analytics (omnibase_infra postgres on .201:5436)
-- Node: node_nightly_loop_controller

-- Persistent decision store: every decision the nightly loop makes
CREATE TABLE IF NOT EXISTS nightly_loop_decisions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id     TEXT UNIQUE NOT NULL,
    iteration_id    TEXT NOT NULL,
    correlation_id  TEXT NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action          TEXT NOT NULL,
    target          TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    model_used      TEXT DEFAULT '',
    cost_usd        NUMERIC(10, 6) DEFAULT 0,
    details         TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: nightly_loop_decisions ----
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

ALTER TABLE nightly_loop_decisions ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE nightly_loop_decisions ADD COLUMN IF NOT EXISTS decision_id TEXT;
ALTER TABLE nightly_loop_decisions ADD COLUMN IF NOT EXISTS iteration_id TEXT;
ALTER TABLE nightly_loop_decisions ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE nightly_loop_decisions ADD COLUMN IF NOT EXISTS timestamp TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE nightly_loop_decisions ADD COLUMN IF NOT EXISTS action TEXT;
ALTER TABLE nightly_loop_decisions ADD COLUMN IF NOT EXISTS target TEXT;
ALTER TABLE nightly_loop_decisions ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE nightly_loop_decisions ADD COLUMN IF NOT EXISTS model_used TEXT DEFAULT '';
ALTER TABLE nightly_loop_decisions ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(10, 6) DEFAULT 0;
ALTER TABLE nightly_loop_decisions ADD COLUMN IF NOT EXISTS details TEXT DEFAULT '';
ALTER TABLE nightly_loop_decisions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'decision_id', 'iteration_id', 'correlation_id', 'timestamp', 'action', 'target', 'outcome', 'created_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'nightly_loop_decisions'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'nightly_loop_decisions'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge nightly_loop_decisions.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'nightly_loop_decisions'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE nightly_loop_decisions ADD CONSTRAINT nightly_loop_decisions_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'nightly_loop_decisions'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['decision_id']::text[]
    ) THEN
        ALTER TABLE nightly_loop_decisions ADD CONSTRAINT nightly_loop_decisions_decision_id_key UNIQUE (decision_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: nightly_loop_decisions ----


CREATE INDEX IF NOT EXISTS idx_nld_correlation_id ON nightly_loop_decisions (correlation_id);
CREATE INDEX IF NOT EXISTS idx_nld_iteration_id ON nightly_loop_decisions (iteration_id);
CREATE INDEX IF NOT EXISTS idx_nld_timestamp ON nightly_loop_decisions (timestamp);
CREATE INDEX IF NOT EXISTS idx_nld_action ON nightly_loop_decisions (action);

-- Iteration history: summary of each loop iteration
CREATE TABLE IF NOT EXISTS nightly_loop_iterations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    iteration_id        TEXT UNIQUE NOT NULL,
    correlation_id      TEXT NOT NULL,
    iteration_number    INT NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    gaps_checked        INT DEFAULT 0,
    gaps_closed         INT DEFAULT 0,
    decisions_made      INT DEFAULT 0,
    tickets_dispatched  INT DEFAULT 0,
    total_cost_usd      NUMERIC(10, 6) DEFAULT 0,
    error               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: nightly_loop_iterations ----
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

ALTER TABLE nightly_loop_iterations ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE nightly_loop_iterations ADD COLUMN IF NOT EXISTS iteration_id TEXT;
ALTER TABLE nightly_loop_iterations ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE nightly_loop_iterations ADD COLUMN IF NOT EXISTS iteration_number INT;
ALTER TABLE nightly_loop_iterations ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE nightly_loop_iterations ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE nightly_loop_iterations ADD COLUMN IF NOT EXISTS gaps_checked INT DEFAULT 0;
ALTER TABLE nightly_loop_iterations ADD COLUMN IF NOT EXISTS gaps_closed INT DEFAULT 0;
ALTER TABLE nightly_loop_iterations ADD COLUMN IF NOT EXISTS decisions_made INT DEFAULT 0;
ALTER TABLE nightly_loop_iterations ADD COLUMN IF NOT EXISTS tickets_dispatched INT DEFAULT 0;
ALTER TABLE nightly_loop_iterations ADD COLUMN IF NOT EXISTS total_cost_usd NUMERIC(10, 6) DEFAULT 0;
ALTER TABLE nightly_loop_iterations ADD COLUMN IF NOT EXISTS error TEXT;
ALTER TABLE nightly_loop_iterations ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'iteration_id', 'correlation_id', 'iteration_number', 'started_at', 'created_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'nightly_loop_iterations'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'nightly_loop_iterations'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge nightly_loop_iterations.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'nightly_loop_iterations'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE nightly_loop_iterations ADD CONSTRAINT nightly_loop_iterations_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'nightly_loop_iterations'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['iteration_id']::text[]
    ) THEN
        ALTER TABLE nightly_loop_iterations ADD CONSTRAINT nightly_loop_iterations_iteration_id_key UNIQUE (iteration_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: nightly_loop_iterations ----


CREATE INDEX IF NOT EXISTS idx_nli_correlation_id ON nightly_loop_iterations (correlation_id);
CREATE INDEX IF NOT EXISTS idx_nli_started_at ON nightly_loop_iterations (started_at);
