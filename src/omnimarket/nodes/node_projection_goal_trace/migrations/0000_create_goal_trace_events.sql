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

-- ---- BEGIN OMN-15376 shape reconciliation: goal_trace_events ----
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS event_id TEXT;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS body_digest TEXT;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS kind TEXT;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS raw_goal_id UUID;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS parent_goal_id UUID;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS goal_id UUID;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS seq INTEGER;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS assignment TEXT;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS id_conflict BOOLEAN DEFAULT FALSE;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS agent_key TEXT;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS parent_agent_key TEXT;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS emitted_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS revision TEXT;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS replaces TEXT;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS verdict TEXT;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS item_hash TEXT;
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.goal_trace_events ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;

-- Static NOT NULL convergence: a column already holding NULLs fails the ALTER, the
-- same outcome as a guard would give, with every relation target visible to the SQL gate.
ALTER TABLE omninode_internal.goal_trace_events ALTER COLUMN event_id SET NOT NULL;
ALTER TABLE omninode_internal.goal_trace_events ALTER COLUMN body_digest SET NOT NULL;
ALTER TABLE omninode_internal.goal_trace_events ALTER COLUMN kind SET NOT NULL;
ALTER TABLE omninode_internal.goal_trace_events ALTER COLUMN id_conflict SET NOT NULL;
ALTER TABLE omninode_internal.goal_trace_events ALTER COLUMN ingested_at SET NOT NULL;
ALTER TABLE omninode_internal.goal_trace_events ALTER COLUMN updated_at SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'omninode_internal.goal_trace_events'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE omninode_internal.goal_trace_events
            ADD CONSTRAINT goal_trace_events_pkey PRIMARY KEY (event_id, body_digest);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'omninode_internal.goal_trace_events'::regclass
          AND contype = 'c' AND conname = 'goal_trace_events_event_id_check'
    ) THEN
        ALTER TABLE omninode_internal.goal_trace_events
            ADD CONSTRAINT goal_trace_events_event_id_check CHECK (btrim(event_id) <> '');
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'omninode_internal.goal_trace_events'::regclass
          AND contype = 'c' AND conname = 'goal_trace_events_body_digest_check'
    ) THEN
        ALTER TABLE omninode_internal.goal_trace_events
            ADD CONSTRAINT goal_trace_events_body_digest_check CHECK (btrim(body_digest) <> '');
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'omninode_internal.goal_trace_events'::regclass
          AND contype = 'c' AND conname = 'goal_trace_events_kind_check'
    ) THEN
        ALTER TABLE omninode_internal.goal_trace_events
            ADD CONSTRAINT goal_trace_events_kind_check CHECK (kind IN (
                'claim', 'revise', 'close', 'bind', 'hook', 'delegation', 'receipt', 'attempt'
            ));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'omninode_internal.goal_trace_events'::regclass
          AND contype = 'c' AND conname = 'goal_trace_events_seq_check'
    ) THEN
        ALTER TABLE omninode_internal.goal_trace_events
            ADD CONSTRAINT goal_trace_events_seq_check CHECK (seq IS NULL OR seq >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'omninode_internal.goal_trace_events'::regclass
          AND contype = 'c' AND conname = 'goal_trace_events_assignment_check'
    ) THEN
        ALTER TABLE omninode_internal.goal_trace_events
            ADD CONSTRAINT goal_trace_events_assignment_check CHECK (assignment IS NULL OR assignment IN (
                'own', 'binding', 'ancestry', 'unassigned', 'ambiguous'
            ));
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: goal_trace_events ----

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

-- ---- BEGIN OMN-15376 shape reconciliation: goal_traces ----
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS goal_id UUID;
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS parent_goal_ids UUID[] DEFAULT '{}';
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS revisions TEXT[] DEFAULT '{}';
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS revision_edges JSONB DEFAULT '[]'::jsonb;
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS forked_revisions TEXT[] DEFAULT '{}';
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS closed_revision TEXT;
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS competing_closes TEXT[] DEFAULT '{}';
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS attempt_ids TEXT[] DEFAULT '{}';
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS current_attempt_id TEXT;
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS verdict TEXT;
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS delegation_ids TEXT[] DEFAULT '{}';
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS receipt_ids TEXT[] DEFAULT '{}';
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS assigned_event_count INTEGER;
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS conflicting_event_ids TEXT[] DEFAULT '{}';
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS checksum TEXT;
ALTER TABLE omninode_internal.goal_traces ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- Static NOT NULL convergence: a column already holding NULLs fails the ALTER, the
-- same outcome as a guard would give, with every relation target visible to the SQL gate.
ALTER TABLE omninode_internal.goal_traces ALTER COLUMN goal_id SET NOT NULL;
ALTER TABLE omninode_internal.goal_traces ALTER COLUMN parent_goal_ids SET NOT NULL;
ALTER TABLE omninode_internal.goal_traces ALTER COLUMN revisions SET NOT NULL;
ALTER TABLE omninode_internal.goal_traces ALTER COLUMN revision_edges SET NOT NULL;
ALTER TABLE omninode_internal.goal_traces ALTER COLUMN forked_revisions SET NOT NULL;
ALTER TABLE omninode_internal.goal_traces ALTER COLUMN competing_closes SET NOT NULL;
ALTER TABLE omninode_internal.goal_traces ALTER COLUMN attempt_ids SET NOT NULL;
ALTER TABLE omninode_internal.goal_traces ALTER COLUMN verdict SET NOT NULL;
ALTER TABLE omninode_internal.goal_traces ALTER COLUMN delegation_ids SET NOT NULL;
ALTER TABLE omninode_internal.goal_traces ALTER COLUMN receipt_ids SET NOT NULL;
ALTER TABLE omninode_internal.goal_traces ALTER COLUMN assigned_event_count SET NOT NULL;
ALTER TABLE omninode_internal.goal_traces ALTER COLUMN conflicting_event_ids SET NOT NULL;
ALTER TABLE omninode_internal.goal_traces ALTER COLUMN checksum SET NOT NULL;
ALTER TABLE omninode_internal.goal_traces ALTER COLUMN updated_at SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'omninode_internal.goal_traces'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE omninode_internal.goal_traces
            ADD CONSTRAINT goal_traces_pkey PRIMARY KEY (goal_id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'omninode_internal.goal_traces'::regclass
          AND contype = 'c' AND conname = 'goal_traces_assigned_event_count_check'
    ) THEN
        ALTER TABLE omninode_internal.goal_traces
            ADD CONSTRAINT goal_traces_assigned_event_count_check CHECK (assigned_event_count >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'omninode_internal.goal_traces'::regclass
          AND contype = 'c' AND conname = 'goal_traces_checksum_check'
    ) THEN
        ALTER TABLE omninode_internal.goal_traces
            ADD CONSTRAINT goal_traces_checksum_check CHECK (checksum LIKE 'sha256:%');
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: goal_traces ----

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
