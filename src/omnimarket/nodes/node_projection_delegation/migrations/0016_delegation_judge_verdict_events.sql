-- OMN-13367: persist controlled, reproducible, auditable judge verdict events
-- for non-verifiable delegation classes.
--
-- The event hash is the idempotency key for replay. Judge failures are stored
-- as typed judge_failed verdicts with actual_score NULL; they are not coerced
-- into a silent 0.0 score.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS delegation_judge_verdict_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_hash TEXT NOT NULL UNIQUE,
    correlation_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    score_source TEXT NOT NULL DEFAULT 'reproducible_judge',
    judge_model TEXT NOT NULL,
    judge_model_version TEXT NOT NULL,
    judge_provider TEXT NOT NULL,
    rubric_id TEXT NOT NULL,
    rubric_hash TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    temperature NUMERIC(5, 3) NOT NULL,
    judge_node_version TEXT NOT NULL,
    reasoning_hash TEXT NOT NULL,
    verdict TEXT NOT NULL,
    actual_score NUMERIC(5, 3),
    failure_kind TEXT,
    failure_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (verdict = 'judge_failed' AND actual_score IS NULL AND failure_kind IS NOT NULL AND failure_message IS NOT NULL)
        OR
        (verdict <> 'judge_failed' AND actual_score IS NOT NULL AND failure_kind IS NULL AND failure_message IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_delegation_judge_verdict_events_correlation_id
    ON delegation_judge_verdict_events (correlation_id);

CREATE INDEX IF NOT EXISTS idx_delegation_judge_verdict_events_verdict
    ON delegation_judge_verdict_events (verdict);
