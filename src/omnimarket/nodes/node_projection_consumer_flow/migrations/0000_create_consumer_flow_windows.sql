-- =============================================================================
-- MIGRATION: per-consumer throughput windows + per-topic production tallies
-- =============================================================================
-- Ticket:  OMN-16777 (Phase 1 of epic OMN-16776 — platform observability)
-- Owner:   omnimarket.nodes.node_projection_consumer_flow
-- Version: 1.0.0
--
-- WHY THIS EXISTS
--   Nothing in the platform measured throughput across a seam. Every liveness
--   signal measured connectedness — group membership, process liveness,
--   container health, LAG — and all four outages on 2026-08-23 were consumers
--   that were connected. The canonical case (OMN-16755):
--   node_gateway_link_health_projection_compute was Stable at LAG 0 with
--   current-offset 15,750 while its declared output topic sat at
--   LOG-END-OFFSET 0. 15,750 in, 0 out, every check green.
--
--   These two tables hold the fact that separates a dead consumer from a quiet
--   one: how many envelopes went in, how many came out, how many were dropped,
--   over one heartbeat window.
--
-- WHY THE COUNTERS ARE NULLABLE
--   A row with messages_in = 0 says "observed, and nothing moved" — that is how
--   IDLE is proven. A row with messages_in = NULL says "this window was never
--   observed" (flow_state = 'UNKNOWN'), which is a different fact. Defaulting
--   these to 0 would let a dropped heartbeat read as a quiet one, which is
--   precisely the false-green this whole epic exists to close (OMN-16777 AC5).
--   That is why there is no DEFAULT 0 here.
--
-- WHY (consumer_group, topic, window_start) IS THE KEY
--   Contract-declared in OMN-16777. window_start is producer-assigned event
--   time, so replaying a window reproduces the same row rather than appending a
--   duplicate. ingest_sequence is the producer's monotonic per-process window
--   counter and is the tie-breaker for ordering — never an ingest clock, which
--   would let a redelivered older window overwrite a newer one.
--
--   Known limitation, recorded rather than hidden: two replicas sharing a
--   consumer group emit distinct window_start values (independent drain
--   timestamps), so they land as distinct rows and the read model aggregates
--   across them. A same-microsecond collision between replicas would resolve
--   last-writer-wins; it is possible and vanishingly unlikely, and no data is
--   summed incorrectly in the normal case.
--
-- WHY ONE TABLE IN public, NOT A public/omninode_internal PAIR
--   node_projection_live_events carries the same read model in BOTH schemas
--   (0000 public + 0002 omninode_internal) because its write path drifted to
--   omninode_internal after public.live_events already existed, and OMN-15819
--   had to reconcile the two. These tables have never existed anywhere, so
--   there is nothing to reconcile and no reason to inherit that split: ONE
--   physical table, in the schema the projection-API read model requires
--   (tests/unit/projection/test_projection_table_migration_coverage.py marks a
--   topic DEGRADED at startup when its declared table is not created here),
--   with db_io declaring the same schema so the runtime write path resolves to
--   the same relation the reader serves. Unqualified CREATE (-> public)
--   matches node_projection_registration's 0000.
-- =============================================================================

CREATE TABLE IF NOT EXISTS consumer_flow_windows (
    consumer_group     TEXT        NOT NULL,
    topic              TEXT        NOT NULL,
    window_start       TIMESTAMPTZ NOT NULL,
    window_end         TIMESTAMPTZ NOT NULL,
    node_id            UUID        NOT NULL,
    ingest_sequence    BIGINT      NOT NULL,

    -- NULL means the window was never observed. NOT the same as 0.
    messages_in        BIGINT,
    messages_out       BIGINT,
    messages_dlq       BIGINT,
    handler_errors     BIGINT,

    -- Envelopes the platform published TO `topic` in an overlapping window.
    -- NULL means no producer of this topic is visible on this rail at all
    -- (an external ingress leg), which is why upstream_evidence exists.
    upstream_produced  BIGINT,
    upstream_evidence  TEXT        NOT NULL,

    -- FLOWING | STALLED | STARVED | IDLE | UNKNOWN. Derived in the projection,
    -- never carried on the producing event (envelope purity).
    flow_state         TEXT        NOT NULL,

    -- Event time (the window's own end), not a wall clock: the row is a
    -- statement about the window, so replay reproduces it byte-identically.
    evaluated_at       TIMESTAMPTZ NOT NULL,

    PRIMARY KEY (consumer_group, topic, window_start)
);

-- The two queries this table exists to answer: "what is not flowing right now"
-- and "what did consumer X do in the last hour".
CREATE INDEX IF NOT EXISTS idx_consumer_flow_windows_state_time
    ON consumer_flow_windows (flow_state, window_end DESC);

CREATE INDEX IF NOT EXISTS idx_consumer_flow_windows_group_time
    ON consumer_flow_windows (consumer_group, window_end DESC);

CREATE TABLE IF NOT EXISTS topic_produce_windows (
    topic              TEXT        NOT NULL,
    window_start       TIMESTAMPTZ NOT NULL,
    window_end         TIMESTAMPTZ NOT NULL,
    node_id            UUID        NOT NULL,
    ingest_sequence    BIGINT      NOT NULL,
    messages_produced  BIGINT      NOT NULL DEFAULT 0,
    evaluated_at       TIMESTAMPTZ NOT NULL,

    PRIMARY KEY (topic, window_start)
);

CREATE INDEX IF NOT EXISTS idx_topic_produce_windows_topic_time
    ON topic_produce_windows (topic, window_end DESC);
