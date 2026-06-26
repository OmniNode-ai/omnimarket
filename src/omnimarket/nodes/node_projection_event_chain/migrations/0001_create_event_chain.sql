-- OMN-13620: node-owned projection migration for the canonical event-chain ledger.
--
-- WHY THIS EXISTS
--   node_projection_event_chain declares projection_api over the event_chain
--   table (topic onex.snapshot.projection.event_chain.v1). It replaces the
--   bespoke SEA EventChainCapture JSON ledger (.onex_state/hackathon/event_chains/
--   {correlation_id}.json) with a queryable, replay-capable canonical projection.
--   One row per ordered (correlation_id, sequence) event. Given a correlation_id,
--   the ordered chain reconstructs deterministically by sorting on sequence; the
--   read-side /projection/{topic} API filters on correlation_id and orders by
--   sequence ASC.
--
--   Discovered + applied by scripts/run-projection-migrations.py (node-owned
--   migrations/ discovery) and vendored to the dashboard projection DB
--   (omnidash_analytics) the projection API binds to.
--
-- Idempotency: CREATE TABLE / INDEX guarded so the migration is safe on a DB
-- where the table already exists and on a fresh omnidash_analytics. The
-- (correlation_id, envelope_id) unique constraint makes the runtime UPSERT
-- replay-safe (a replayed canonical event overwrites its own row, never appends).

-- ============================================================================
-- EVENT_CHAIN TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS event_chain (
    correlation_id VARCHAR(256) NOT NULL,
    envelope_id VARCHAR(256) NOT NULL,

    sequence INTEGER NOT NULL DEFAULT 0,
    topic TEXT NOT NULL DEFAULT '',
    source_node TEXT NOT NULL DEFAULT 'unknown',
    causation_id VARCHAR(256) NOT NULL DEFAULT '',
    captured_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_event_chain PRIMARY KEY (correlation_id, envelope_id),
    CONSTRAINT non_negative_event_chain_sequence CHECK (sequence >= 0)
);

-- Ordered chain reconstruction: filter by correlation_id, order by sequence.
CREATE INDEX IF NOT EXISTS idx_event_chain_correlation_sequence
    ON event_chain (correlation_id, sequence);

CREATE INDEX IF NOT EXISTS idx_event_chain_captured_at
    ON event_chain (captured_at DESC);
