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
