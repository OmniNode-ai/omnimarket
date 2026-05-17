-- OMN-10754: session_outcomes projection table.
-- Target DB: omnidash_analytics (omnibase_infra postgres on .201:5436)
-- Node: node_projection_session_outcome
-- UPSERT key: session_id (latest-state-wins)

CREATE TABLE IF NOT EXISTS session_outcomes (
    session_id  TEXT PRIMARY KEY,
    outcome     TEXT NOT NULL,
    emitted_at  TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_session_outcomes_emitted_at
    ON session_outcomes (emitted_at);

CREATE INDEX IF NOT EXISTS idx_session_outcomes_outcome
    ON session_outcomes (outcome);

CREATE OR REPLACE FUNCTION refresh_session_outcomes_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_session_outcomes_updated_at ON session_outcomes;
CREATE TRIGGER trg_session_outcomes_updated_at
    BEFORE UPDATE ON session_outcomes
    FOR EACH ROW
    EXECUTE FUNCTION refresh_session_outcomes_updated_at();
