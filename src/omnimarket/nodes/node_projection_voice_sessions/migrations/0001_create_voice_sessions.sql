-- OMN-13086: voice_sessions projection table.
-- Target DB: omnidash_analytics (omnibase_infra postgres on .201:5436)
-- Node: node_projection_voice_sessions
-- UPSERT key: session_id (latest-state-wins)
-- Snapshot topic: onex.snapshot.projection.voice.sessions.v1

CREATE TABLE IF NOT EXISTS voice_sessions (
    session_id          TEXT PRIMARY KEY,
    started_at          TIMESTAMPTZ NOT NULL,
    ended_at            TIMESTAMPTZ,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    total_turns         INTEGER NOT NULL DEFAULT 0 CHECK (total_turns >= 0),
    total_duration_ms   BIGINT NOT NULL DEFAULT 0 CHECK (total_duration_ms >= 0),
    agent_name          TEXT NOT NULL DEFAULT '',
    transcript_turns    JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(transcript_turns) = 'array'),
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE INDEX IF NOT EXISTS idx_voice_sessions_started_at
    ON voice_sessions (started_at DESC);

CREATE INDEX IF NOT EXISTS idx_voice_sessions_is_active
    ON voice_sessions (is_active);

CREATE INDEX IF NOT EXISTS idx_voice_sessions_agent_name
    ON voice_sessions (agent_name);

CREATE OR REPLACE FUNCTION refresh_voice_sessions_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_voice_sessions_updated_at ON voice_sessions;
CREATE TRIGGER trg_voice_sessions_updated_at
    BEFORE UPDATE ON voice_sessions
    FOR EACH ROW
    EXECUTE FUNCTION refresh_voice_sessions_updated_at();
