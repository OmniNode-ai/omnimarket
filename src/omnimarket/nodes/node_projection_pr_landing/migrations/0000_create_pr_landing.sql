-- =============================================================================
-- MIGRATION: PR landing workflow read models
-- =============================================================================
-- Ticket:  OMN-19833 (task T11 of the PR landing workflow plan, epic OMN-19822)
-- Owner:   omnimarket.nodes.node_projection_pr_landing
-- Version: 1.0.0
--
-- WHAT THIS HOLDS
--   node_pr_landing_orchestrator publishes four events: one per transition
--   (with the row's per-key seq), one when only an agent can move the PR, and
--   one per terminal (merged, closed). The orchestrator's own state_io row is
--   its private durable state; these two tables are the read models the drain,
--   the dashboard and the success metric read instead.
--
--     pr_landing_state        one row per (repository, pr_number): where the PR
--                             is now, in which episode, and why an agent is
--                             needed or when it ended
--     pr_landing_transitions  one row per (repository, pr_number, seq): every
--                             transition the orchestrator took, append-only
--
-- WHY seq IS THE ORDERING AUTHORITY
--   seq is assigned by the orchestrator in the same compare-and-set that writes
--   its row, so it is a total order over one PR's transitions. An event time
--   is not: the outbox is at-least-once, and it can emit an older episode's
--   closed after a newer merged (F10). The writer's upsert refuses a write
--   whose seq is below the stored one IN THE CONFLICT ARM'S WHERE, not in a
--   read-then-write, which two consumers could race.
--
-- WHY THE SAME seq CAN BE WRITTEN TWICE
--   A transition into NEEDS_AGENT, MERGED or CLOSED is described by two
--   events on two topics that share its seq: the transition and its
--   agent-needed or terminal event. They can arrive in either order, and each
--   carries columns the other lacks (the trigger; the reason or the episode).
--   So an equal seq is accepted when it adds the half the row has not seen:
--   a transition is refused only when the row already holds a transition at
--   that seq (last_trigger is set), and an agent-needed or terminal event at
--   that seq re-asserts values it alone carries.
--
-- WHY episode IS NOT NULL WITH NO PRODUCER COLUMN
--   The transition event carries no episode. A terminal does (F9, F10: a
--   terminal is keyed (PR, episode), and a reopened PR carries one terminal per
--   episode), and a transition out of CLOSED is the table's reopen, which adds
--   one. A row first seen mid-life starts at 0 and is corrected by the next
--   terminal. Residual, documented rather than hidden: a reopen transition
--   refused as older than an agent-needed event of a later seq leaves the
--   episode one low until the next terminal re-asserts it.
--
-- Idempotency: CREATE TABLE IF NOT EXISTS plus one guarded ADD COLUMN per
-- declared column. Nothing here touches RLS, ownership or any role attribute.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.pr_landing_state (
    repository         TEXT        NOT NULL,
    pr_number          INTEGER     NOT NULL,
    seq                BIGINT      NOT NULL,
    state              TEXT        NOT NULL,
    head_sha           TEXT,
    episode            INTEGER     NOT NULL DEFAULT 0,
    last_trigger       TEXT,
    agent_reason       TEXT,
    agent_detail       TEXT,
    terminal_at        TIMESTAMPTZ,
    event_at           TIMESTAMPTZ NOT NULL,
    first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor  BIGSERIAL   NOT NULL,

    CONSTRAINT pk_pr_landing_state
        PRIMARY KEY (repository, pr_number),
    CONSTRAINT ck_pr_landing_state_state
        CHECK (state IN (
            'OBSERVED',
            'PARKED',
            'COMPANION_PENDING',
            'COMPANION_OPEN',
            'CHECKS_PENDING',
            'READY',
            'ARMED',
            'NEEDS_AGENT',
            'MERGED',
            'CLOSED'
        )),
    CONSTRAINT ck_pr_landing_state_agent_reason
        CHECK (agent_reason IS NULL OR agent_reason IN (
            'companion_declined',
            'companion_error',
            'real_red',
            'stalled'
        )),
    CONSTRAINT ck_pr_landing_state_seq_non_negative
        CHECK (seq >= 0),
    CONSTRAINT ck_pr_landing_state_episode_non_negative
        CHECK (episode >= 0)
);

ALTER TABLE omninode_internal.pr_landing_state
    ADD COLUMN IF NOT EXISTS repository        TEXT;
ALTER TABLE omninode_internal.pr_landing_state
    ADD COLUMN IF NOT EXISTS pr_number         INTEGER;
ALTER TABLE omninode_internal.pr_landing_state
    ADD COLUMN IF NOT EXISTS seq               BIGINT;
ALTER TABLE omninode_internal.pr_landing_state
    ADD COLUMN IF NOT EXISTS state             TEXT;
ALTER TABLE omninode_internal.pr_landing_state
    ADD COLUMN IF NOT EXISTS head_sha          TEXT;
ALTER TABLE omninode_internal.pr_landing_state
    ADD COLUMN IF NOT EXISTS episode           INTEGER DEFAULT 0;
ALTER TABLE omninode_internal.pr_landing_state
    ADD COLUMN IF NOT EXISTS last_trigger      TEXT;
ALTER TABLE omninode_internal.pr_landing_state
    ADD COLUMN IF NOT EXISTS agent_reason      TEXT;
ALTER TABLE omninode_internal.pr_landing_state
    ADD COLUMN IF NOT EXISTS agent_detail      TEXT;
ALTER TABLE omninode_internal.pr_landing_state
    ADD COLUMN IF NOT EXISTS terminal_at       TIMESTAMPTZ;
ALTER TABLE omninode_internal.pr_landing_state
    ADD COLUMN IF NOT EXISTS event_at          TIMESTAMPTZ;
ALTER TABLE omninode_internal.pr_landing_state
    ADD COLUMN IF NOT EXISTS first_seen_at     TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.pr_landing_state
    ADD COLUMN IF NOT EXISTS updated_at        TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.pr_landing_state
    ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_pr_landing_state_projection_cursor
    ON omninode_internal.pr_landing_state (projection_cursor);

-- The drain's access path: every PR that only an agent can move.
CREATE INDEX IF NOT EXISTS idx_pr_landing_state_state
    ON omninode_internal.pr_landing_state (state);

CREATE TABLE IF NOT EXISTS omninode_internal.pr_landing_transitions (
    repository         TEXT        NOT NULL,
    pr_number          INTEGER     NOT NULL,
    seq                BIGINT      NOT NULL,
    head_sha           TEXT,
    from_state         TEXT,
    to_state           TEXT        NOT NULL,
    trigger            TEXT        NOT NULL,
    intents            JSONB       NOT NULL DEFAULT '[]'::jsonb,
    opens_episode      BOOLEAN     NOT NULL DEFAULT FALSE,
    transitioned_at    TIMESTAMPTZ NOT NULL,
    first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor  BIGSERIAL   NOT NULL,

    CONSTRAINT pk_pr_landing_transitions
        PRIMARY KEY (repository, pr_number, seq),
    CONSTRAINT ck_pr_landing_transitions_from_state
        CHECK (from_state IS NULL OR from_state IN (
            'OBSERVED',
            'PARKED',
            'COMPANION_PENDING',
            'COMPANION_OPEN',
            'CHECKS_PENDING',
            'READY',
            'ARMED',
            'NEEDS_AGENT',
            'MERGED',
            'CLOSED'
        )),
    CONSTRAINT ck_pr_landing_transitions_to_state
        CHECK (to_state IN (
            'OBSERVED',
            'PARKED',
            'COMPANION_PENDING',
            'COMPANION_OPEN',
            'CHECKS_PENDING',
            'READY',
            'ARMED',
            'NEEDS_AGENT',
            'MERGED',
            'CLOSED'
        )),
    CONSTRAINT ck_pr_landing_transitions_seq_non_negative
        CHECK (seq >= 0)
);

ALTER TABLE omninode_internal.pr_landing_transitions
    ADD COLUMN IF NOT EXISTS repository        TEXT;
ALTER TABLE omninode_internal.pr_landing_transitions
    ADD COLUMN IF NOT EXISTS pr_number         INTEGER;
ALTER TABLE omninode_internal.pr_landing_transitions
    ADD COLUMN IF NOT EXISTS seq               BIGINT;
ALTER TABLE omninode_internal.pr_landing_transitions
    ADD COLUMN IF NOT EXISTS head_sha          TEXT;
ALTER TABLE omninode_internal.pr_landing_transitions
    ADD COLUMN IF NOT EXISTS from_state        TEXT;
ALTER TABLE omninode_internal.pr_landing_transitions
    ADD COLUMN IF NOT EXISTS to_state          TEXT;
ALTER TABLE omninode_internal.pr_landing_transitions
    ADD COLUMN IF NOT EXISTS trigger           TEXT;
ALTER TABLE omninode_internal.pr_landing_transitions
    ADD COLUMN IF NOT EXISTS intents           JSONB DEFAULT '[]'::jsonb;
ALTER TABLE omninode_internal.pr_landing_transitions
    ADD COLUMN IF NOT EXISTS opens_episode     BOOLEAN DEFAULT FALSE;
ALTER TABLE omninode_internal.pr_landing_transitions
    ADD COLUMN IF NOT EXISTS transitioned_at   TIMESTAMPTZ;
ALTER TABLE omninode_internal.pr_landing_transitions
    ADD COLUMN IF NOT EXISTS first_seen_at     TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.pr_landing_transitions
    ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_pr_landing_transitions_projection_cursor
    ON omninode_internal.pr_landing_transitions (projection_cursor);

CREATE INDEX IF NOT EXISTS idx_pr_landing_transitions_transitioned_at
    ON omninode_internal.pr_landing_transitions (transitioned_at DESC);

COMMENT ON TABLE omninode_internal.pr_landing_state IS
    'OMN-19833: one row per (repository, pull request) for the PR landing workflow. '
    'seq, the orchestrator''s per-key sequence, is the only ordering authority; a stale '
    'write is refused in the upsert WHERE. Written only by node_projection_pr_landing.';

COMMENT ON TABLE omninode_internal.pr_landing_transitions IS
    'OMN-19833: every transition node_pr_landing_orchestrator took, one row per '
    '(repository, pull request, seq). Append-only: a redelivery inserts nothing and '
    'no row is ever updated. Written only by node_projection_pr_landing.';
