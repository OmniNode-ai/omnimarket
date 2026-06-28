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
