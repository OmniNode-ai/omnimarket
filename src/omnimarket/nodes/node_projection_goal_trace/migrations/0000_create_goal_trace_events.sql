-- =============================================================================
-- MIGRATION: goal_trace_events and goal_traces
-- =============================================================================
-- Ticket: OMN-20034 (GC.11 of the delegation judgeability plan).
-- Owner:  omnimarket.nodes.node_projection_goal_trace
-- Version: 1.0.0
--
-- WHY TWO TABLES
--   goal_trace_events holds one row per distinct (event id, body digest) the
--   trace absorbed, content-free: identifiers, times and digests only. A
--   redelivery has the same id and digest and converges on the row it wrote; the
--   same id with another digest is a different row and both are kept, which is
--   how an id conflict stays visible.
--   goal_traces holds one folded row per goal: the revision chain, the closed
--   revision, the verdict, the checksum over the canonically ordered event set.
--
-- WHY goal_id IS NULLABLE ON AN EVENT ROW
--   An event of an agent with no binding yet is stored with a NULL goal_id and
--   is joined when the binding arrives, in either order. raw_goal_id is the
--   goal the SOURCE event named (the stamped binding event, a claim, a close);
--   goal_id is the goal the fold attached the row to. The two differ for a hook
--   joined through its agent's binding, and the fold needs the raw one to
--   rebuild its input.
--
-- WHY omninode_internal, EXPLICITLY QUALIFIED
--   The application database domain gate rejects an unqualified relation.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.goal_trace_events (
    event_id          TEXT        NOT NULL CHECK (btrim(event_id) <> ''),
    body_digest       TEXT        NOT NULL CHECK (btrim(body_digest) <> ''),
    kind              TEXT        NOT NULL CHECK (kind IN (
        'claim', 'revise', 'close', 'bind', 'hook', 'delegation', 'receipt', 'attempt'
    )),
    raw_goal_id       UUID,
    parent_goal_id    UUID,
    goal_id           UUID,
    seq               INTEGER     CHECK (seq IS NULL OR seq >= 0),
    assignment        TEXT        CHECK (assignment IS NULL OR assignment IN (
        'own', 'binding', 'ancestry', 'unassigned', 'ambiguous'
    )),
    id_conflict       BOOLEAN     NOT NULL DEFAULT FALSE,
    session_id        TEXT,
    agent_key         TEXT,
    parent_agent_key  TEXT,
    emitted_at        TIMESTAMPTZ,
    revision          TEXT,
    replaces          TEXT,
    completed_at      TIMESTAMPTZ,
    verdict           TEXT,
    item_hash         TEXT,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor BIGSERIAL,
    PRIMARY KEY (event_id, body_digest)
);

CREATE INDEX IF NOT EXISTS idx_goal_trace_events_goal
    ON omninode_internal.goal_trace_events (goal_id, seq)
    WHERE goal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_goal_trace_events_raw_goal
    ON omninode_internal.goal_trace_events (raw_goal_id)
    WHERE raw_goal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_goal_trace_events_session
    ON omninode_internal.goal_trace_events (session_id)
    WHERE session_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS omninode_internal.goal_traces (
    goal_id               UUID        PRIMARY KEY,
    parent_goal_ids       UUID[]      NOT NULL DEFAULT '{}',
    revisions             TEXT[]      NOT NULL DEFAULT '{}',
    revision_edges        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    forked_revisions      TEXT[]      NOT NULL DEFAULT '{}',
    closed_revision       TEXT,
    competing_closes      TEXT[]      NOT NULL DEFAULT '{}',
    attempt_ids           TEXT[]      NOT NULL DEFAULT '{}',
    current_attempt_id    TEXT,
    verdict               TEXT        NOT NULL,
    delegation_ids        TEXT[]      NOT NULL DEFAULT '{}',
    receipt_ids           TEXT[]      NOT NULL DEFAULT '{}',
    assigned_event_count  INTEGER     NOT NULL CHECK (assigned_event_count >= 0),
    conflicting_event_ids TEXT[]      NOT NULL DEFAULT '{}',
    checksum              TEXT        NOT NULL CHECK (checksum LIKE 'sha256:%'),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- A column that exists with the wrong shape is rejected: division by zero makes
-- ON_ERROR_STOP abort the file.
SELECT 1 / count(*) AS goal_trace_events_goal_id_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'goal_trace_events'
   AND column_name = 'goal_id'
   AND data_type = 'uuid'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS goal_trace_events_primary_key_assertion
  FROM information_schema.table_constraints
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'goal_trace_events'
   AND constraint_type = 'PRIMARY KEY';

SELECT 1 / count(*) AS goal_traces_checksum_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'goal_traces'
   AND column_name = 'checksum'
   AND is_nullable = 'NO';
