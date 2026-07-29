-- OMN-13088 (NC-15): Singleton snapshot table for delegation inference response text.
-- Closes the coverage gap where omnidash declared
-- onex.snapshot.projection.delegation.inference-response-text.v1 but no
-- reducer was producing it (node_projection_delegation and
-- node_llm_delegation_projection publish_topics verified not to include this topic).
--
-- One row per deployment (singleton_key = 'global').  Each new inference-response
-- event from onex.evt.omnibase-infra.inference-response.v1 upserts this row,
-- rolling the recent_responses JSONB window and updating latest_* scalars.

CREATE TABLE IF NOT EXISTS projection_delegation_inference_response_text (
    -- Singleton anchor: always 'global'
    singleton_key             TEXT PRIMARY KEY,

    -- Latest scalar fields from the most recent inference-response event
    latest_correlation_id     TEXT    NOT NULL DEFAULT '',
    latest_model_name         TEXT    NOT NULL DEFAULT '',
    -- task_type is not carried by ModelInferenceResponseData; defaults to empty
    latest_task_type          TEXT    NOT NULL DEFAULT '',
    latest_generated_text     TEXT    NOT NULL DEFAULT '',
    latest_prompt_tokens      INT     NOT NULL DEFAULT 0 CHECK (latest_prompt_tokens >= 0),
    latest_completion_tokens  INT     NOT NULL DEFAULT 0 CHECK (latest_completion_tokens >= 0),
    latest_latency_ms         INT     NOT NULL DEFAULT 0 CHECK (latest_latency_ms >= 0),

    -- The Kafka topic that feeds this projection
    source_topic              TEXT    NOT NULL
        DEFAULT 'onex.evt.omnibase-infra.inference-response.v1',

    -- Rolling FIFO window of recent responses (max MAX_HISTORY = 10)
    -- Each entry: {correlation_id, model_name, task_type, generated_text,
    --              prompt_tokens, completion_tokens, latency_ms, captured_at}
    recent_responses          JSONB   NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(recent_responses) = 'array'),

    -- When this snapshot was last materialized
    captured_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- True once the projection reducer has written at least one row
    provisioned               BOOLEAN NOT NULL DEFAULT TRUE,

    CHECK (singleton_key = 'global')
);

-- ---- BEGIN OMN-15376 shape reconciliation: projection_delegation_inference_response_text ----
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

ALTER TABLE projection_delegation_inference_response_text ADD COLUMN IF NOT EXISTS singleton_key TEXT;
ALTER TABLE projection_delegation_inference_response_text ADD COLUMN IF NOT EXISTS latest_correlation_id TEXT DEFAULT '';
ALTER TABLE projection_delegation_inference_response_text ADD COLUMN IF NOT EXISTS latest_model_name TEXT DEFAULT '';
ALTER TABLE projection_delegation_inference_response_text ADD COLUMN IF NOT EXISTS latest_task_type TEXT DEFAULT '';
ALTER TABLE projection_delegation_inference_response_text ADD COLUMN IF NOT EXISTS latest_generated_text TEXT DEFAULT '';
ALTER TABLE projection_delegation_inference_response_text ADD COLUMN IF NOT EXISTS latest_prompt_tokens INT DEFAULT 0;
ALTER TABLE projection_delegation_inference_response_text ADD COLUMN IF NOT EXISTS latest_completion_tokens INT DEFAULT 0;
ALTER TABLE projection_delegation_inference_response_text ADD COLUMN IF NOT EXISTS latest_latency_ms INT DEFAULT 0;
ALTER TABLE projection_delegation_inference_response_text ADD COLUMN IF NOT EXISTS source_topic TEXT DEFAULT 'onex.evt.omnibase-infra.inference-response.v1';
ALTER TABLE projection_delegation_inference_response_text ADD COLUMN IF NOT EXISTS recent_responses JSONB DEFAULT '[]'::jsonb;
ALTER TABLE projection_delegation_inference_response_text ADD COLUMN IF NOT EXISTS captured_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE projection_delegation_inference_response_text ADD COLUMN IF NOT EXISTS provisioned BOOLEAN DEFAULT TRUE;

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['singleton_key', 'latest_correlation_id', 'latest_model_name', 'latest_task_type', 'latest_generated_text', 'latest_prompt_tokens', 'latest_completion_tokens', 'latest_latency_ms', 'source_topic', 'recent_responses', 'captured_at', 'provisioned']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'projection_delegation_inference_response_text'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'projection_delegation_inference_response_text'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge projection_delegation_inference_response_text.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'projection_delegation_inference_response_text'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE projection_delegation_inference_response_text ADD CONSTRAINT projection_delegation_inference_response_text_pkey PRIMARY KEY (singleton_key);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'projection_delegation_inference_response_text'::regclass AND conname = 'projection_delegation_inference_resp_latest_prompt_tokens_check'
    ) THEN
        ALTER TABLE projection_delegation_inference_response_text ADD CONSTRAINT projection_delegation_inference_resp_latest_prompt_tokens_check CHECK (latest_prompt_tokens >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'projection_delegation_inference_response_text'::regclass AND conname = 'projection_delegation_inference__latest_completion_tokens_check'
    ) THEN
        ALTER TABLE projection_delegation_inference_response_text ADD CONSTRAINT projection_delegation_inference__latest_completion_tokens_check CHECK (latest_completion_tokens >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'projection_delegation_inference_response_text'::regclass AND conname = 'projection_delegation_inference_respons_latest_latency_ms_check'
    ) THEN
        ALTER TABLE projection_delegation_inference_response_text ADD CONSTRAINT projection_delegation_inference_respons_latest_latency_ms_check CHECK (latest_latency_ms >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'projection_delegation_inference_response_text'::regclass AND conname = 'projection_delegation_inference_response_recent_responses_check'
    ) THEN
        ALTER TABLE projection_delegation_inference_response_text ADD CONSTRAINT projection_delegation_inference_response_recent_responses_check CHECK (jsonb_typeof(recent_responses) = 'array');
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'projection_delegation_inference_response_text'::regclass AND conname = 'projection_delegation_inference_response_te_singleton_key_check'
    ) THEN
        ALTER TABLE projection_delegation_inference_response_text ADD CONSTRAINT projection_delegation_inference_response_te_singleton_key_check CHECK (singleton_key = 'global');
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: projection_delegation_inference_response_text ----


-- Seed the singleton row so the projection-API always returns a row
-- (avoids 404 before the first inference-response event arrives).
INSERT INTO projection_delegation_inference_response_text
    (singleton_key, source_topic, provisioned, recent_responses)
VALUES
    ('global',
     'onex.evt.omnibase-infra.inference-response.v1',
     FALSE,
     '[]'::jsonb)
ON CONFLICT (singleton_key) DO NOTHING;
