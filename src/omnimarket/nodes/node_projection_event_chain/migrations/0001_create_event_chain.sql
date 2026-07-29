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

-- ---- BEGIN OMN-15376 shape reconciliation: event_chain ----
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

ALTER TABLE event_chain ADD COLUMN IF NOT EXISTS correlation_id VARCHAR(256);
ALTER TABLE event_chain ADD COLUMN IF NOT EXISTS envelope_id VARCHAR(256);
ALTER TABLE event_chain ADD COLUMN IF NOT EXISTS sequence INTEGER DEFAULT 0;
ALTER TABLE event_chain ADD COLUMN IF NOT EXISTS topic TEXT DEFAULT '';
ALTER TABLE event_chain ADD COLUMN IF NOT EXISTS source_node TEXT DEFAULT 'unknown';
ALTER TABLE event_chain ADD COLUMN IF NOT EXISTS causation_id VARCHAR(256) DEFAULT '';
ALTER TABLE event_chain ADD COLUMN IF NOT EXISTS captured_at TIMESTAMPTZ;
ALTER TABLE event_chain ADD COLUMN IF NOT EXISTS payload JSONB DEFAULT '{}'::JSONB;
ALTER TABLE event_chain ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['correlation_id', 'envelope_id', 'sequence', 'topic', 'source_node', 'causation_id', 'captured_at', 'payload', 'created_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'event_chain'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'event_chain'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge event_chain.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'event_chain'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE event_chain ADD CONSTRAINT pk_event_chain PRIMARY KEY (correlation_id, envelope_id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'event_chain'::regclass AND conname = 'non_negative_event_chain_sequence'
    ) THEN
        ALTER TABLE event_chain ADD CONSTRAINT non_negative_event_chain_sequence CHECK (sequence >= 0);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: event_chain ----


-- Ordered chain reconstruction: filter by correlation_id, order by sequence.
CREATE INDEX IF NOT EXISTS idx_event_chain_correlation_sequence
    ON event_chain (correlation_id, sequence);

CREATE INDEX IF NOT EXISTS idx_event_chain_captured_at
    ON event_chain (captured_at DESC);
