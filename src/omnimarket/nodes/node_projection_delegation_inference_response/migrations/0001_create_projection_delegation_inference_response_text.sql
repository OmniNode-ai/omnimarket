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
    latest_prompt_tokens      INT     NOT NULL DEFAULT 0,
    latest_completion_tokens  INT     NOT NULL DEFAULT 0,
    latest_latency_ms         INT     NOT NULL DEFAULT 0,

    -- The Kafka topic that feeds this projection
    source_topic              TEXT    NOT NULL
        DEFAULT 'onex.evt.omnibase-infra.inference-response.v1',

    -- Rolling FIFO window of recent responses (max MAX_HISTORY = 10)
    -- Each entry: {correlation_id, model_name, task_type, generated_text,
    --              prompt_tokens, completion_tokens, latency_ms, captured_at}
    recent_responses          JSONB   NOT NULL DEFAULT '[]'::jsonb,

    -- When this snapshot was last materialized
    captured_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- True once the projection reducer has written at least one row
    provisioned               BOOLEAN NOT NULL DEFAULT TRUE
);

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
