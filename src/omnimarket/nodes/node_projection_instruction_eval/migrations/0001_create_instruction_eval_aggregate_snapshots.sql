-- OMN-12998: node-owned projection migration for instruction_eval_aggregate_snapshots.
--
-- WHY THIS EXISTS
--   node_projection_instruction_eval declares projection_api over
--   instruction_eval_aggregate_snapshots (topic
--   onex.snapshot.projection.omnimarket.instruction-eval-aggregate.v1).
--   The omnidash InstructionEvalHeatmap panel reads this topic via
--   useProjectionQuery; rows materialise as the instruction-eval runner emits
--   onex.evt.omnimarket.instruction-eval-result.v1 events.
--
--   Until rows exist the panel renders an honest empty/degraded state
--   (em-dash cells, no fixture). This replaces the hardcoded
--   instruction-eval.fixtures.ts committed data (run 20260526-170241).
--
-- Idempotency: CREATE TABLE / INDEX / TRIGGER guarded so the migration is safe
-- on a DB where the table already exists and on a fresh omnidash_analytics.

-- ============================================================================
-- INSTRUCTION_EVAL_AGGREGATE_SNAPSHOTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS instruction_eval_aggregate_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    model VARCHAR(256) NOT NULL,
    task VARCHAR(256) NOT NULL,
    context_mode VARCHAR(64) NOT NULL,

    -- mean pass rate 0-1 across `runs`; NULL when no data yet rather than fake 0
    pass_rate NUMERIC(6, 4),
    -- mean output tokens across `runs`
    output_tokens INTEGER NOT NULL DEFAULT 0,
    -- number of eval runs aggregated
    runs INTEGER NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_instruction_eval_aggregate_model_task_mode
        UNIQUE (model, task, context_mode),

    CONSTRAINT chk_instruction_eval_aggregate_pass_rate
        CHECK (pass_rate IS NULL OR (pass_rate >= 0 AND pass_rate <= 1)),

    CONSTRAINT chk_instruction_eval_aggregate_output_tokens
        CHECK (output_tokens >= 0),

    CONSTRAINT chk_instruction_eval_aggregate_runs
        CHECK (runs >= 0)
);

-- ---- BEGIN OMN-15376 shape reconciliation: instruction_eval_aggregate_snapshots ----
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

ALTER TABLE instruction_eval_aggregate_snapshots ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE instruction_eval_aggregate_snapshots ADD COLUMN IF NOT EXISTS model VARCHAR(256);
ALTER TABLE instruction_eval_aggregate_snapshots ADD COLUMN IF NOT EXISTS task VARCHAR(256);
ALTER TABLE instruction_eval_aggregate_snapshots ADD COLUMN IF NOT EXISTS context_mode VARCHAR(64);
ALTER TABLE instruction_eval_aggregate_snapshots ADD COLUMN IF NOT EXISTS pass_rate NUMERIC(6, 4);
ALTER TABLE instruction_eval_aggregate_snapshots ADD COLUMN IF NOT EXISTS output_tokens INTEGER DEFAULT 0;
ALTER TABLE instruction_eval_aggregate_snapshots ADD COLUMN IF NOT EXISTS runs INTEGER DEFAULT 0;
ALTER TABLE instruction_eval_aggregate_snapshots ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE instruction_eval_aggregate_snapshots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'model', 'task', 'context_mode', 'output_tokens', 'runs', 'created_at', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'instruction_eval_aggregate_snapshots'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'instruction_eval_aggregate_snapshots'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge instruction_eval_aggregate_snapshots.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'instruction_eval_aggregate_snapshots'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE instruction_eval_aggregate_snapshots ADD CONSTRAINT instruction_eval_aggregate_snapshots_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'instruction_eval_aggregate_snapshots'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['context_mode', 'model', 'task']::text[]
    ) THEN
        ALTER TABLE instruction_eval_aggregate_snapshots ADD CONSTRAINT uq_instruction_eval_aggregate_model_task_mode UNIQUE (model, task, context_mode);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'instruction_eval_aggregate_snapshots'::regclass AND conname = 'chk_instruction_eval_aggregate_pass_rate'
    ) THEN
        ALTER TABLE instruction_eval_aggregate_snapshots ADD CONSTRAINT chk_instruction_eval_aggregate_pass_rate CHECK (pass_rate IS NULL OR (pass_rate >= 0 AND pass_rate <= 1));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'instruction_eval_aggregate_snapshots'::regclass AND conname = 'chk_instruction_eval_aggregate_output_tokens'
    ) THEN
        ALTER TABLE instruction_eval_aggregate_snapshots ADD CONSTRAINT chk_instruction_eval_aggregate_output_tokens CHECK (output_tokens >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'instruction_eval_aggregate_snapshots'::regclass AND conname = 'chk_instruction_eval_aggregate_runs'
    ) THEN
        ALTER TABLE instruction_eval_aggregate_snapshots ADD CONSTRAINT chk_instruction_eval_aggregate_runs CHECK (runs >= 0);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: instruction_eval_aggregate_snapshots ----


CREATE INDEX IF NOT EXISTS idx_instruction_eval_aggregate_snapshots_model
    ON instruction_eval_aggregate_snapshots (model);

CREATE INDEX IF NOT EXISTS idx_instruction_eval_aggregate_snapshots_task
    ON instruction_eval_aggregate_snapshots (task);

CREATE INDEX IF NOT EXISTS idx_instruction_eval_aggregate_snapshots_updated_at
    ON instruction_eval_aggregate_snapshots (updated_at DESC);

-- ============================================================================
-- TRIGGER: auto-update updated_at
-- ============================================================================
CREATE OR REPLACE FUNCTION update_instruction_eval_aggregate_snapshots_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_instruction_eval_aggregate_snapshots_updated_at
    ON instruction_eval_aggregate_snapshots;

CREATE TRIGGER trigger_instruction_eval_aggregate_snapshots_updated_at
    BEFORE UPDATE ON instruction_eval_aggregate_snapshots
    FOR EACH ROW
    EXECUTE FUNCTION update_instruction_eval_aggregate_snapshots_updated_at();
