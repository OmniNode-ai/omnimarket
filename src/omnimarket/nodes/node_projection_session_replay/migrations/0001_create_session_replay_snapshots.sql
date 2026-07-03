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
