-- OMN-13087: session_replay_snapshots projection table.
-- Target DB: omnidash_analytics (omnibase_infra postgres on .201:5436)
-- Node: node_projection_session_replay
-- UPSERT key: snapshot_id (deterministic UUID from session_id + sequence)
--
-- WHY THIS EXISTS
--   omnidash declares topic onex.snapshot.projection.session.replay.v1 in
--   shared/types/topics.ts. The SessionReplayPage widget renders rows from
--   this projection. Without a matching table + reducer contract the topic
--   is DEGRADED at projection API startup. This migration creates the table
--   that node_projection_session_replay materialises into.
--
--   Vendored by omnibase_infra/scripts/sync-node-migrations.sh into
--   docker/migrations/forward/nodes/node_projection_session_replay/ and
--   applied to NODE_POSTGRES_DB (omnidash_analytics) by
--   run-forward-migrations.sh under the namespaced migration id
--   node:node_projection_session_replay:<file>.
--
-- Idempotency: CREATE TABLE / INDEX guarded by IF NOT EXISTS.

-- ============================================================================
-- SESSION_REPLAY_SNAPSHOTS TABLE
-- ============================================================================
-- One row per session event, ordered by (session_id, sequence).
CREATE TABLE IF NOT EXISTS public.session_replay_snapshots (
    snapshot_id         TEXT             PRIMARY KEY,
    session_id          TEXT             NOT NULL,
    sequence            INT              NOT NULL DEFAULT 0,
    timestamp           TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    event_type          TEXT             NOT NULL,
    node_name           TEXT             NOT NULL DEFAULT '',
    state_delta         JSONB            NOT NULL DEFAULT '{}',
    cumulative_tokens   INT              NOT NULL DEFAULT 0,
    is_checkpoint       BOOLEAN          NOT NULL DEFAULT FALSE,
    ingested_at         TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    UNIQUE (session_id, sequence)
);

-- ---- BEGIN OMN-15376 shape reconciliation: session_replay_snapshots ----
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

ALTER TABLE public.session_replay_snapshots ADD COLUMN IF NOT EXISTS snapshot_id TEXT;
ALTER TABLE public.session_replay_snapshots ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE public.session_replay_snapshots ADD COLUMN IF NOT EXISTS sequence INT DEFAULT 0;
ALTER TABLE public.session_replay_snapshots ADD COLUMN IF NOT EXISTS timestamp TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE public.session_replay_snapshots ADD COLUMN IF NOT EXISTS event_type TEXT;
ALTER TABLE public.session_replay_snapshots ADD COLUMN IF NOT EXISTS node_name TEXT DEFAULT '';
ALTER TABLE public.session_replay_snapshots ADD COLUMN IF NOT EXISTS state_delta JSONB DEFAULT '{}';
ALTER TABLE public.session_replay_snapshots ADD COLUMN IF NOT EXISTS cumulative_tokens INT DEFAULT 0;
ALTER TABLE public.session_replay_snapshots ADD COLUMN IF NOT EXISTS is_checkpoint BOOLEAN DEFAULT FALSE;
ALTER TABLE public.session_replay_snapshots ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['snapshot_id', 'session_id', 'sequence', 'timestamp', 'event_type', 'node_name', 'state_delta', 'cumulative_tokens', 'is_checkpoint', 'ingested_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'public.session_replay_snapshots'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'public.session_replay_snapshots'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge public.session_replay_snapshots.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.session_replay_snapshots'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.session_replay_snapshots ADD CONSTRAINT session_replay_snapshots_pkey PRIMARY KEY (snapshot_id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'public.session_replay_snapshots'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['sequence', 'session_id']::text[]
    ) THEN
        ALTER TABLE public.session_replay_snapshots ADD CONSTRAINT session_replay_snapshots_session_id_sequence_key UNIQUE (session_id, sequence);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: session_replay_snapshots ----


-- Primary lookup: all snapshots for a session in order.
CREATE INDEX IF NOT EXISTS idx_session_replay_session_sequence
    ON public.session_replay_snapshots (session_id, sequence ASC);

-- Freshness: most-recently ingested snapshot first.
CREATE INDEX IF NOT EXISTS idx_session_replay_ingested_at
    ON public.session_replay_snapshots (ingested_at DESC);

-- Checkpoint fast-path: filter to checkpoint rows only.
CREATE INDEX IF NOT EXISTS idx_session_replay_is_checkpoint
    ON public.session_replay_snapshots (session_id, is_checkpoint)
    WHERE is_checkpoint = TRUE;
