-- OMN-13083: node-owned projection migration for traces.
--
-- WHY THIS EXISTS
--   node_projection_traces declares projection_api over the traces table
--   (topic onex.snapshot.projection.traces.v1). The dashboard trace-explorer
--   widget's projectionSchema requires the correlation-grouped row shape
--   (correlation_id, nodes_involved, event_count, first_event_at,
--   last_event_at, duration_ms, has_error, is_running, latest_message).
--   No contract previously backed that topic; OMN-12135 wired it to a bespoke
--   Express query and OMN-12822 removed that bespoke server. This node-owned
--   table is the canonical backing surface for the projection API.
--
--   Discovered + applied by scripts/run-projection-migrations.py (node-owned
--   migrations/ discovery) and vendored to the dashboard projection DB
--   (omnidash_analytics) the projection API binds to.
--
-- Idempotency: CREATE TABLE / INDEX / TRIGGER guarded so the migration is safe
-- on a DB where the table already exists and on a fresh omnidash_analytics.

-- ============================================================================
-- TRACES TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS traces (
    correlation_id VARCHAR(256) PRIMARY KEY,

    nodes_involved TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    event_count INTEGER NOT NULL DEFAULT 0,

    first_event_at TIMESTAMPTZ NOT NULL,
    last_event_at TIMESTAMPTZ NOT NULL,
    duration_ms BIGINT NOT NULL DEFAULT 0,

    has_error BOOLEAN NOT NULL DEFAULT FALSE,
    is_running BOOLEAN NOT NULL DEFAULT TRUE,
    latest_message TEXT NOT NULL DEFAULT '',

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT non_negative_traces_event_count CHECK (event_count >= 0),
    CONSTRAINT non_negative_traces_duration_ms CHECK (duration_ms >= 0)
);

CREATE INDEX IF NOT EXISTS idx_traces_last_event_at
    ON traces (last_event_at DESC);

CREATE INDEX IF NOT EXISTS idx_traces_has_error
    ON traces (has_error);

CREATE INDEX IF NOT EXISTS idx_traces_is_running
    ON traces (is_running);

-- ============================================================================
-- TRIGGER: auto-update updated_at
-- ============================================================================
CREATE OR REPLACE FUNCTION update_traces_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_traces_updated_at ON traces;
CREATE TRIGGER trigger_traces_updated_at
    BEFORE UPDATE ON traces
    FOR EACH ROW
    EXECUTE FUNCTION update_traces_updated_at();
