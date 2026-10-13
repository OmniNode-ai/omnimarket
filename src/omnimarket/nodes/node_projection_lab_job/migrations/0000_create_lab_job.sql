-- =============================================================================
-- MIGRATION: Lab job supervisor read models
-- =============================================================================
-- Ticket:  OMN-20604 (plan step M2 of the lab job supervisor)
-- Owner:   omnimarket.nodes.node_projection_lab_job
-- Version: 1.0.0
--
-- WHAT THIS HOLDS
--   node_lab_job_orchestrator (M3) publishes one event for each reducer
--   transition, carrying the complete post-transition row and CAS-assigned seq.
--   Its private state_io row remains the orchestrator's durable authority;
--   these tables are the read models used by the dashboard and supervision.
--
--     lab_job_state        one row per job_id: state, episode, attempt, owner,
--                          liveness and outcome, original spec and lane context
--     lab_job_transitions  one row per (job_id, seq): every transition taken,
--                          append-only even when it arrives after a newer state
--
-- WHY seq IS THE ORDERING AUTHORITY
--   seq is assigned in the orchestrator's compare-and-set. Event time is not
--   an order: the outbox is at-least-once and can redeliver older transitions.
--   Equal and lower seqs are refused IN THE CONFLICT ARM'S WHERE, never a
--   read-then-write that two consumers could race. Each event contains the
--   complete row, so there is no second half to merge at an equal seq.
--
-- WHY spec IS NULLABLE
--   An adopted lane can lack its original recorded brief. The projection
--   preserves that NULL rather than inventing a spec or continuation brief.
--
-- Idempotency: CREATE TABLE IF NOT EXISTS plus one guarded ADD COLUMN per
-- declared column. Nothing here touches RLS, ownership or any role attribute.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.lab_job_state (
    job_id               TEXT        NOT NULL,
    kind                 TEXT        NOT NULL,
    state                TEXT        NOT NULL,
    episode              INTEGER     NOT NULL DEFAULT 1,
    attempt              INTEGER     NOT NULL DEFAULT 1,
    seq                  BIGINT      NOT NULL,
    entered_state_at     TIMESTAMPTZ NOT NULL,
    claimed_at           TIMESTAMPTZ,
    next_dispatch_at     TIMESTAMPTZ,
    owner_runtime        TEXT,
    work_unit_id         TEXT,
    run_id               TEXT,
    last_verdict         TEXT,
    last_outcome         TEXT,
    time_box_hit         BOOLEAN     NOT NULL DEFAULT FALSE,
    continuation         TEXT,
    failure_reason       TEXT,
    parent_lane          TEXT,
    ticket               TEXT,
    alert_sent_at        TIMESTAMPTZ,
    spec                 JSONB,
    first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor    BIGSERIAL   NOT NULL,

    CONSTRAINT pk_lab_job_state
        PRIMARY KEY (job_id),
    CONSTRAINT ck_lab_job_state_state
        CHECK (state IN (
            'queued',
            'dispatched',
            'running',
            'checking',
            'stopping',
            'retrying',
            'stalled',
            'failed',
            'alerting',
            'alerted',
            'done'
        )),
    CONSTRAINT ck_lab_job_state_seq_positive
        CHECK (seq >= 1),
    CONSTRAINT ck_lab_job_state_attempt_positive
        CHECK (attempt >= 1),
    CONSTRAINT ck_lab_job_state_episode_positive
        CHECK (episode >= 1)
);

ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS job_id               TEXT;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS kind                 TEXT;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS state                TEXT;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS episode              INTEGER DEFAULT 1;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS attempt              INTEGER DEFAULT 1;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS seq                  BIGINT;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS entered_state_at     TIMESTAMPTZ;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS claimed_at           TIMESTAMPTZ;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS next_dispatch_at     TIMESTAMPTZ;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS owner_runtime        TEXT;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS work_unit_id         TEXT;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS run_id               TEXT;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS last_verdict         TEXT;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS last_outcome         TEXT;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS time_box_hit         BOOLEAN DEFAULT FALSE;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS continuation         TEXT;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS failure_reason       TEXT;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS parent_lane          TEXT;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS ticket               TEXT;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS alert_sent_at        TIMESTAMPTZ;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS spec                 JSONB;
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS first_seen_at        TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS updated_at           TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.lab_job_state
    ADD COLUMN IF NOT EXISTS projection_cursor    BIGSERIAL;

CREATE TABLE IF NOT EXISTS omninode_internal.lab_job_transitions (
    job_id               TEXT        NOT NULL,
    seq                  BIGINT      NOT NULL,
    from_state           TEXT,
    to_state             TEXT        NOT NULL,
    attempt              INTEGER     NOT NULL,
    at                   TIMESTAMPTZ NOT NULL,
    reason               TEXT        NOT NULL,
    recorded_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_lab_job_transitions
        PRIMARY KEY (job_id, seq),
    CONSTRAINT ck_lab_job_transitions_from_state
        CHECK (from_state IS NULL OR from_state IN (
            'queued',
            'dispatched',
            'running',
            'checking',
            'stopping',
            'retrying',
            'stalled',
            'failed',
            'alerting',
            'alerted',
            'done'
        )),
    CONSTRAINT ck_lab_job_transitions_to_state
        CHECK (to_state IN (
            'queued',
            'dispatched',
            'running',
            'checking',
            'stopping',
            'retrying',
            'stalled',
            'failed',
            'alerting',
            'alerted',
            'done'
        )),
    CONSTRAINT ck_lab_job_transitions_seq_positive
        CHECK (seq >= 1),
    CONSTRAINT ck_lab_job_transitions_attempt_positive
        CHECK (attempt >= 1)
);

ALTER TABLE omninode_internal.lab_job_transitions
    ADD COLUMN IF NOT EXISTS job_id               TEXT;
ALTER TABLE omninode_internal.lab_job_transitions
    ADD COLUMN IF NOT EXISTS seq                  BIGINT;
ALTER TABLE omninode_internal.lab_job_transitions
    ADD COLUMN IF NOT EXISTS from_state           TEXT;
ALTER TABLE omninode_internal.lab_job_transitions
    ADD COLUMN IF NOT EXISTS to_state             TEXT;
ALTER TABLE omninode_internal.lab_job_transitions
    ADD COLUMN IF NOT EXISTS attempt              INTEGER;
ALTER TABLE omninode_internal.lab_job_transitions
    ADD COLUMN IF NOT EXISTS at                   TIMESTAMPTZ;
ALTER TABLE omninode_internal.lab_job_transitions
    ADD COLUMN IF NOT EXISTS reason               TEXT;
ALTER TABLE omninode_internal.lab_job_transitions
    ADD COLUMN IF NOT EXISTS recorded_at          TIMESTAMPTZ DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS idx_lab_job_state_projection_cursor
    ON omninode_internal.lab_job_state (projection_cursor);

-- The supervisor's access path: jobs waiting for a dispatch or intervention.
CREATE INDEX IF NOT EXISTS idx_lab_job_state_state
    ON omninode_internal.lab_job_state (state);

CREATE INDEX IF NOT EXISTS idx_lab_job_transitions_at
    ON omninode_internal.lab_job_transitions (at DESC);

COMMENT ON TABLE omninode_internal.lab_job_state IS
    'OMN-20604 M2: one complete post-transition row per lab job. The CAS-assigned '
    'seq is the only ordering authority; equal and stale writes are refused in '
    'the upsert WHERE. Written only by node_projection_lab_job.';

COMMENT ON TABLE omninode_internal.lab_job_transitions IS
    'OMN-20604 M2: every reducer transition, one row per (job_id, seq). '
    'Append-only: a redelivery inserts nothing and no row is ever updated. '
    'Written only by node_projection_lab_job.';
