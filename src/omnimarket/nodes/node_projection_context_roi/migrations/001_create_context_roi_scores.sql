-- OMN-12955: Context-ROI experiment scores projection table.
--
-- Backs the projection topic
--   onex.snapshot.projection.context.experiment-scores.v1
-- consumed by the omnidash /experiments ContextExperimentHero and
-- ContextEffectivenessHeatmap panels.
--
-- Source: per-(task x arm x trial) ModelAttemptReductionRow instances carried
-- on the runner terminal event onex.evt.omnimarket.context-roi-run-completed.v1.
-- One projection row per (run_id, task_id, context_factor_subset, correlation_id)
-- cell. Identity is the runner correlation_id which is unique per cell.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS context_roi_scores (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Identity / correlation
    run_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_order INTEGER NOT NULL DEFAULT 0 CHECK (run_order >= 0),
    -- Arm / context pack (the heatmap "segment")
    context_factor_subset TEXT NOT NULL DEFAULT 'off',
    context_pack_hash TEXT NOT NULL DEFAULT '',
    -- Generation telemetry
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    first_pass_success BOOLEAN NOT NULL DEFAULT FALSE,
    final_success BOOLEAN NOT NULL DEFAULT FALSE,
    failure_stage TEXT NOT NULL DEFAULT 'none',
    -- Token accounting
    prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    tokens_used INTEGER NOT NULL DEFAULT 0 CHECK (tokens_used >= 0),
    estimated_cost NUMERIC(18, 6) NOT NULL DEFAULT 0 CHECK (estimated_cost >= 0),
    -- Model / routing identity
    model_id TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    endpoint_ref TEXT NOT NULL DEFAULT '',
    -- Evidence classification
    proof_class TEXT NOT NULL DEFAULT 'runtime-observed-only',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: context_roi_scores ----
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

ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS run_id TEXT;
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS task_id TEXT;
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS run_order INTEGER DEFAULT 0;
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS context_factor_subset TEXT DEFAULT 'off';
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS context_pack_hash TEXT DEFAULT '';
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS attempt_count INTEGER DEFAULT 0;
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS first_pass_success BOOLEAN DEFAULT FALSE;
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS final_success BOOLEAN DEFAULT FALSE;
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS failure_stage TEXT DEFAULT 'none';
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS prompt_tokens INTEGER DEFAULT 0;
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS completion_tokens INTEGER DEFAULT 0;
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS tokens_used INTEGER DEFAULT 0;
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS estimated_cost NUMERIC(18, 6) DEFAULT 0;
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS model_id TEXT DEFAULT '';
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS provider TEXT DEFAULT '';
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS endpoint_ref TEXT DEFAULT '';
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS proof_class TEXT DEFAULT 'runtime-observed-only';
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE context_roi_scores ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'run_id', 'correlation_id', 'task_id', 'run_order', 'context_factor_subset', 'context_pack_hash', 'attempt_count', 'first_pass_success', 'final_success', 'failure_stage', 'prompt_tokens', 'completion_tokens', 'tokens_used', 'estimated_cost', 'model_id', 'provider', 'endpoint_ref', 'proof_class', 'created_at', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'context_roi_scores'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'context_roi_scores'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge context_roi_scores.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'context_roi_scores'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE context_roi_scores ADD CONSTRAINT context_roi_scores_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'context_roi_scores'::regclass AND conname = 'context_roi_scores_run_order_check'
    ) THEN
        ALTER TABLE context_roi_scores ADD CONSTRAINT context_roi_scores_run_order_check CHECK (run_order >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'context_roi_scores'::regclass AND conname = 'context_roi_scores_attempt_count_check'
    ) THEN
        ALTER TABLE context_roi_scores ADD CONSTRAINT context_roi_scores_attempt_count_check CHECK (attempt_count >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'context_roi_scores'::regclass AND conname = 'context_roi_scores_prompt_tokens_check'
    ) THEN
        ALTER TABLE context_roi_scores ADD CONSTRAINT context_roi_scores_prompt_tokens_check CHECK (prompt_tokens >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'context_roi_scores'::regclass AND conname = 'context_roi_scores_completion_tokens_check'
    ) THEN
        ALTER TABLE context_roi_scores ADD CONSTRAINT context_roi_scores_completion_tokens_check CHECK (completion_tokens >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'context_roi_scores'::regclass AND conname = 'context_roi_scores_tokens_used_check'
    ) THEN
        ALTER TABLE context_roi_scores ADD CONSTRAINT context_roi_scores_tokens_used_check CHECK (tokens_used >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'context_roi_scores'::regclass AND conname = 'context_roi_scores_estimated_cost_check'
    ) THEN
        ALTER TABLE context_roi_scores ADD CONSTRAINT context_roi_scores_estimated_cost_check CHECK (estimated_cost >= 0);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: context_roi_scores ----


CREATE UNIQUE INDEX IF NOT EXISTS ux_context_roi_scores_identity
    ON context_roi_scores (correlation_id);

CREATE INDEX IF NOT EXISTS ix_context_roi_scores_run
    ON context_roi_scores (run_id);

CREATE INDEX IF NOT EXISTS ix_context_roi_scores_segment_model
    ON context_roi_scores (context_factor_subset, model_id);

CREATE OR REPLACE FUNCTION refresh_context_roi_scores_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_context_roi_scores_updated_at ON context_roi_scores;
CREATE TRIGGER trg_context_roi_scores_updated_at
    BEFORE UPDATE ON context_roi_scores
    FOR EACH ROW
    EXECUTE FUNCTION refresh_context_roi_scores_updated_at();
