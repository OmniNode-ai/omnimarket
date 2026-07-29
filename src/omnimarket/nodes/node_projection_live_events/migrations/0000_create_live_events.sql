-- OMN-13079: Create the live_events projection table.
-- DDL owner: omnimarket.nodes.node_projection_live_events
-- Do not add a duplicate CREATE TABLE migration for this table in omnibase_infra.
--
-- Purpose: stores recent platform-wide bus events for the omnidash
-- live-event-stream widget (onex.snapshot.projection.live-events.v1).
-- Dedup key: event_id (UNIQUE). Retention: latest 1000 rows enforced
-- by the projection handler after each UPSERT.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS live_events (
  id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id       TEXT        UNIQUE NOT NULL,
  type           TEXT        NOT NULL DEFAULT 'ACTION',
  timestamp      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  source         TEXT        NOT NULL DEFAULT 'platform',
  topic          TEXT        NOT NULL DEFAULT '',
  summary        TEXT        NOT NULL DEFAULT '',
  payload        TEXT        NOT NULL DEFAULT '{}',
  correlation_id TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: live_events ----
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

ALTER TABLE live_events ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE live_events ADD COLUMN IF NOT EXISTS event_id TEXT;
ALTER TABLE live_events ADD COLUMN IF NOT EXISTS type TEXT DEFAULT 'ACTION';
ALTER TABLE live_events ADD COLUMN IF NOT EXISTS timestamp TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE live_events ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'platform';
ALTER TABLE live_events ADD COLUMN IF NOT EXISTS topic TEXT DEFAULT '';
ALTER TABLE live_events ADD COLUMN IF NOT EXISTS summary TEXT DEFAULT '';
ALTER TABLE live_events ADD COLUMN IF NOT EXISTS payload TEXT DEFAULT '{}';
ALTER TABLE live_events ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE live_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'event_id', 'type', 'timestamp', 'source', 'topic', 'summary', 'payload', 'created_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'live_events'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'live_events'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge live_events.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'live_events'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE live_events ADD CONSTRAINT live_events_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'live_events'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['event_id']::text[]
    ) THEN
        ALTER TABLE live_events ADD CONSTRAINT live_events_event_id_key UNIQUE (event_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: live_events ----


CREATE INDEX IF NOT EXISTS idx_live_events_created_at
  ON live_events (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_live_events_topic
  ON live_events (topic);

CREATE INDEX IF NOT EXISTS idx_live_events_source
  ON live_events (source);

CREATE INDEX IF NOT EXISTS idx_live_events_correlation_id
  ON live_events (correlation_id)
  WHERE correlation_id IS NOT NULL;

COMMENT ON TABLE live_events IS
  'Platform-wide bus event projection — feeds the omnidash live-event-stream widget.';
