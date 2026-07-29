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

-- ---- BEGIN OMN-15376 shape reconciliation: delegation_judge_verdict_events ----
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

ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS event_hash TEXT;
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS task_type TEXT;
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS score_source TEXT DEFAULT 'reproducible_judge';
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS judge_model TEXT;
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS judge_model_version TEXT;
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS judge_provider TEXT;
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS rubric_id TEXT;
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS rubric_hash TEXT;
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS prompt_hash TEXT;
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS input_hash TEXT;
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS temperature NUMERIC(5, 3);
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS judge_node_version TEXT;
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS reasoning_hash TEXT;
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS verdict TEXT;
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS actual_score NUMERIC(5, 3);
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS failure_kind TEXT;
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS failure_message TEXT;
ALTER TABLE delegation_judge_verdict_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'event_hash', 'correlation_id', 'task_type', 'score_source', 'judge_model', 'judge_model_version', 'judge_provider', 'rubric_id', 'rubric_hash', 'prompt_hash', 'input_hash', 'temperature', 'judge_node_version', 'reasoning_hash', 'verdict', 'created_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'delegation_judge_verdict_events'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'delegation_judge_verdict_events'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge delegation_judge_verdict_events.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'delegation_judge_verdict_events'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE delegation_judge_verdict_events ADD CONSTRAINT delegation_judge_verdict_events_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'delegation_judge_verdict_events'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['event_hash']::text[]
    ) THEN
        ALTER TABLE delegation_judge_verdict_events ADD CONSTRAINT delegation_judge_verdict_events_event_hash_key UNIQUE (event_hash);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'delegation_judge_verdict_events'::regclass AND conname = 'delegation_judge_verdict_events_check'
    ) THEN
        ALTER TABLE delegation_judge_verdict_events ADD CONSTRAINT delegation_judge_verdict_events_check CHECK ( (verdict = 'judge_failed' AND actual_score IS NULL AND failure_kind IS NOT NULL AND failure_message IS NOT NULL) OR (verdict <> 'judge_failed' AND actual_score IS NOT NULL AND failure_kind IS NULL AND failure_message IS NULL) );
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: delegation_judge_verdict_events ----


CREATE INDEX IF NOT EXISTS idx_delegation_judge_verdict_events_correlation_id
    ON delegation_judge_verdict_events (correlation_id);

CREATE INDEX IF NOT EXISTS idx_delegation_judge_verdict_events_verdict
    ON delegation_judge_verdict_events (verdict);
