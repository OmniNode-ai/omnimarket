-- OMN-13078: intent_classification_events projection table.
-- Target DB: omnidash_analytics (omnibase_infra postgres on .201:5436)
-- Node: node_projection_intent_classification
-- UPSERT key: correlation_id (latest-wins)
-- Feeds omnidash widgets: intent-distribution, session-timeline

CREATE TABLE IF NOT EXISTS intent_classification_events (
    id             BIGSERIAL PRIMARY KEY,
    correlation_id TEXT UNIQUE NOT NULL,
    session_id     TEXT NOT NULL,
    intent_class   TEXT NOT NULL,
    confidence     FLOAT NOT NULL DEFAULT 0.0,
    keywords       TEXT[] NOT NULL DEFAULT '{}',
    emitted_at     TIMESTAMPTZ NOT NULL,
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_intent_classification_events_session_id
    ON intent_classification_events (session_id);

CREATE INDEX IF NOT EXISTS idx_intent_classification_events_intent_class
    ON intent_classification_events (intent_class);

CREATE INDEX IF NOT EXISTS idx_intent_classification_events_emitted_at
    ON intent_classification_events (emitted_at);

CREATE OR REPLACE FUNCTION refresh_intent_classification_events_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_intent_classification_events_updated_at
    ON intent_classification_events;
CREATE TRIGGER trg_intent_classification_events_updated_at
    BEFORE UPDATE ON intent_classification_events
    FOR EACH ROW
    EXECUTE FUNCTION refresh_intent_classification_events_updated_at();
