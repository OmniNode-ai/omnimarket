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

-- ---- BEGIN OMN-15376 shape reconciliation: voice_sessions ----
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

ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ;
ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS total_turns INTEGER DEFAULT 0;
ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS total_duration_ms BIGINT DEFAULT 0;
ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS agent_name TEXT DEFAULT '';
ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS transcript_turns JSONB DEFAULT '[]'::jsonb;
ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['session_id', 'started_at', 'is_active', 'total_turns', 'total_duration_ms', 'agent_name', 'transcript_turns', 'ingested_at', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'voice_sessions'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'voice_sessions'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge voice_sessions.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'voice_sessions'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE voice_sessions ADD CONSTRAINT voice_sessions_pkey PRIMARY KEY (session_id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'voice_sessions'::regclass AND conname = 'voice_sessions_total_turns_check'
    ) THEN
        ALTER TABLE voice_sessions ADD CONSTRAINT voice_sessions_total_turns_check CHECK (total_turns >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'voice_sessions'::regclass AND conname = 'voice_sessions_total_duration_ms_check'
    ) THEN
        ALTER TABLE voice_sessions ADD CONSTRAINT voice_sessions_total_duration_ms_check CHECK (total_duration_ms >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'voice_sessions'::regclass AND conname = 'voice_sessions_transcript_turns_check'
    ) THEN
        ALTER TABLE voice_sessions ADD CONSTRAINT voice_sessions_transcript_turns_check CHECK (jsonb_typeof(transcript_turns) = 'array');
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'voice_sessions'::regclass AND conname = 'voice_sessions_check'
    ) THEN
        ALTER TABLE voice_sessions ADD CONSTRAINT voice_sessions_check CHECK (ended_at IS NULL OR ended_at >= started_at);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: voice_sessions ----


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
