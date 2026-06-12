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
