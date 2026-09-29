-- =============================================================================
-- MIGRATION: omninode_internal.claude_hook_events and claude_agent_spans
-- =============================================================================
-- Ticket: OMN-19513 (all-hooks capture, build task 3: the consumer half).
-- Contract: omniclaude src/omniclaude/hooks/contracts/contract_hook_claude_capture.yaml,
--           section `projection` (event_table, lineage_table, fold_rules).
-- Version: 1.0.0
--
-- WHY THIS FILE EXISTS
--   The two existing hook tables cannot tell a subagent's events from the main
--   thread's. Subagents share their parent's session_id (measured on the lab,
--   2026-09-26), the four legacy hook topics carry no agent_id or tool_use_id,
--   and hook_events stores the session id in a column named run_id. The capture
--   contract adds one metadata topic for all 33 Claude Code hook types, carrying
--   lineage on every event. These two tables are its read model:
--
--   claude_hook_events   one row per captured hook event, metadata only, with
--                        session_id as a real column and lineage promoted to
--                        columns. The payload column holds the metadata-only
--                        payload; content is referenced by content_ref_ids and
--                        lives only in the restricted content-captured family.
--   claude_agent_spans   one row per (session, subagent): who spawned it, how
--                        that was established (parent_resolution), when it ran,
--                        and how many tool calls it completed.
--
-- WHY omninode_internal AND NOT public
--   A net-new relation names its schema explicitly (the OMN-15361 application
--   database SQL gate), and the legacy default schema's exemption list exists
--   for pre-existing relations only. Same placement as work_events and
--   runner_fleet_liveness.
--
-- WHY EVERY PRECONDITION IS A BARE SELECT
--   The application-database SQL gate rejects any new DO $$ ... $$ block in a
--   migration touching an application-topology schema. Every assertion below
--   uses the statically provable `SELECT 1 / count(*)` idiom: a false condition
--   raises division by zero and ON_ERROR_STOP aborts the file.
--
-- IDEMPOTENCY
--   CREATE TABLE / CREATE INDEX are IF NOT EXISTS; every probe is read-only.
--   Re-running this file is a no-op. No DROP, no TRUNCATE, no unguarded ALTER.
--   Grants live in 0001, after these shape assertions, so a rejected table
--   never receives the runtime's write grant.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Preconditions: the schema exists, and the connecting role can create in
--    it and re-grant USAGE onward (0001 grants USAGE to omninode_runtime, and
--    Postgres silently no-ops an onward grant without the grant option).
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS omninode_internal_schema_exists_precondition
  FROM pg_catalog.pg_namespace
 WHERE nspname = 'omninode_internal';

SELECT 1 / count(*) AS omninode_internal_schema_privilege_precondition
  FROM (
    SELECT 1
     WHERE has_schema_privilege(current_user, 'omninode_internal', 'USAGE')
       AND has_schema_privilege(current_user, 'omninode_internal', 'CREATE')
       AND has_schema_privilege(
             current_user, 'omninode_internal', 'USAGE WITH GRANT OPTION'
           )
  ) AS assertion;

-- -----------------------------------------------------------------------------
-- 2. The event table.
--
--    event_id           The producer's deterministic uuid5 (session, agent,
--                       hook, tool_use_id, prompt_id, emitted_at). A repeat is
--                       a replay of the same event, so the writer inserts with
--                       ON CONFLICT DO NOTHING.
--    session_id         The Claude Code session. Shared by every subagent of
--                       the session, so it is never the discriminator alone.
--    agent_id           NULL means the main thread. The ONLY field that tells
--                       a subagent's event from the main thread's.
--    is_subagent        Carried from the wire; constrained below to equal
--                       (agent_id IS NOT NULL), as the contract states.
--    parent_tool_use_id The Agent/Task tool call that spawned this subagent,
--                       from the harness sidecar at emit time.
--    parent_agent_id    Derived by the fold: the agent that made that call.
--                       NULL for a main-thread parent AND for an unresolved
--                       one; claude_agent_spans.parent_resolution separates
--                       the two.
--    causation_id       For a tool completion, the PreToolUse's tool_call_key;
--                       joins the pair without trusting emit timestamps.
--    emitted_at         Producer event time. Display order only.
--    payload            The metadata-only payload, verbatim. Never content.
--    content_ref_ids    The restricted content records this event references.
--    ingested_at        Projection-side write time; minus emitted_at is lag.
--    projection_cursor  Database-assigned ingest order: the stable page
--                       boundary and the cursor a consumer resumes from.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS omninode_internal.claude_hook_events (
    event_id            UUID        NOT NULL,
    session_id          TEXT        NOT NULL,
    agent_id            TEXT,
    is_subagent         BOOLEAN     NOT NULL,
    agent_type          TEXT,
    parent_tool_use_id  TEXT,
    parent_agent_id     TEXT,
    workflow_run_id     TEXT,
    spawn_depth         INTEGER,
    hook_event_name     TEXT        NOT NULL,
    tool_use_id         TEXT,
    tool_name           TEXT,
    prompt_id           TEXT,
    turn_id             TEXT,
    correlation_id      UUID        NOT NULL,
    causation_id        UUID,
    emitted_at          TIMESTAMPTZ NOT NULL,
    payload             JSONB       NOT NULL DEFAULT '{}'::jsonb,
    content_ref_ids     TEXT[]      NOT NULL DEFAULT '{}'::text[],
    source_topic        TEXT        NOT NULL,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor   BIGSERIAL   NOT NULL,
    CONSTRAINT pk_claude_hook_events PRIMARY KEY (event_id),
    CONSTRAINT ck_claude_hook_events_subagent_flag
        CHECK (is_subagent = (agent_id IS NOT NULL))
);

-- -----------------------------------------------------------------------------
-- 3. The span table: one row per (session, subagent).
--
--    parent_resolution  main_thread | resolved | workflow | unknown. Exists
--                       because a NULL parent_agent_id alone cannot tell
--                       "spawned by the main thread" from "we could not tell".
--                       unknown is repaired in place when the spawning call
--                       arrives after the child's events.
--    tool_call_count    PostToolUse plus PostToolUseFailure events carrying
--                       this agent_id, RECOUNTED from claude_hook_events on
--                       every write, so a redelivery cannot count twice.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS omninode_internal.claude_agent_spans (
    session_id          TEXT        NOT NULL,
    agent_id            TEXT        NOT NULL,
    agent_type          TEXT,
    parent_tool_use_id  TEXT,
    parent_agent_id     TEXT,
    parent_resolution   TEXT        NOT NULL,
    workflow_run_id     TEXT,
    spawn_depth         INTEGER,
    started_at          TIMESTAMPTZ NOT NULL,
    stopped_at          TIMESTAMPTZ,
    tool_call_count     INTEGER     NOT NULL DEFAULT 0,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor   BIGSERIAL   NOT NULL,
    CONSTRAINT pk_claude_agent_spans PRIMARY KEY (session_id, agent_id),
    CONSTRAINT ck_claude_agent_spans_parent_resolution
        CHECK (parent_resolution IN ('main_thread', 'resolved', 'workflow', 'unknown'))
);

-- ---- BEGIN OMN-15376 shape reconciliation: omninode_internal.claude_hook_events ----
-- CREATE TABLE IF NOT EXISTS silently no-ops over a same-named table of a
-- different shape. Every column declared above is covered by a guarded ADD
-- COLUMN; each is a no-op on the fresh-create path. ADD COLUMN cannot apply a
-- key or NOT NULL retroactively, so section 5 asserts the shape afterwards.
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS event_id           UUID;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS session_id         TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS agent_id           TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS is_subagent        BOOLEAN;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS agent_type         TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS parent_tool_use_id TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS parent_agent_id    TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS workflow_run_id    TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS spawn_depth        INTEGER;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS hook_event_name    TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS tool_use_id        TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS tool_name          TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS prompt_id          TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS turn_id            TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS correlation_id     UUID;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS causation_id       UUID;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS emitted_at         TIMESTAMPTZ;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS payload            JSONB DEFAULT '{}'::jsonb;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS content_ref_ids    TEXT[] DEFAULT '{}'::text[];
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS source_topic       TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS ingested_at        TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS projection_cursor  BIGSERIAL;
-- ---- END OMN-15376 shape reconciliation: omninode_internal.claude_hook_events ----

-- ---- BEGIN OMN-15376 shape reconciliation: omninode_internal.claude_agent_spans ----
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS session_id         TEXT;
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS agent_id           TEXT;
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS agent_type         TEXT;
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS parent_tool_use_id TEXT;
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS parent_agent_id    TEXT;
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS parent_resolution  TEXT;
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS workflow_run_id    TEXT;
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS spawn_depth        INTEGER;
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS started_at         TIMESTAMPTZ;
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS stopped_at         TIMESTAMPTZ;
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS tool_call_count    INTEGER DEFAULT 0;
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS first_seen_at      TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS updated_at         TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS projection_cursor  BIGSERIAL;
-- ---- END OMN-15376 shape reconciliation: omninode_internal.claude_agent_spans ----

-- -----------------------------------------------------------------------------
-- 4. Indexes, one per access path the writer and the readers take.
-- -----------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS idx_claude_hook_events_projection_cursor
    ON omninode_internal.claude_hook_events (projection_cursor);

-- A session's timeline, and one agent's timeline inside it.
CREATE INDEX IF NOT EXISTS idx_claude_hook_events_session_emitted
    ON omninode_internal.claude_hook_events (session_id, emitted_at);

CREATE INDEX IF NOT EXISTS idx_claude_hook_events_session_agent_emitted
    ON omninode_internal.claude_hook_events (session_id, agent_id, emitted_at);

-- The writer's parent lookup: the spawning PreToolUse by (session, call).
CREATE INDEX IF NOT EXISTS idx_claude_hook_events_pre_tool_call
    ON omninode_internal.claude_hook_events (session_id, tool_use_id)
    WHERE hook_event_name = 'PreToolUse';

-- The out-of-order repair: children of one spawning call.
CREATE INDEX IF NOT EXISTS idx_claude_hook_events_parent_call
    ON omninode_internal.claude_hook_events (session_id, parent_tool_use_id)
    WHERE parent_tool_use_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_claude_hook_events_emitted
    ON omninode_internal.claude_hook_events (emitted_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_claude_agent_spans_projection_cursor
    ON omninode_internal.claude_agent_spans (projection_cursor);

CREATE INDEX IF NOT EXISTS idx_claude_agent_spans_parent_call
    ON omninode_internal.claude_agent_spans (session_id, parent_tool_use_id)
    WHERE parent_tool_use_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 5. Shape post-conditions. Exact column sets, not "some key exists": a key on
--    the wrong column would admit duplicates while passing a looser check.
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS claude_hook_events_primary_key_assertion
  FROM (
    SELECT tc.constraint_name
      FROM information_schema.table_constraints tc
      JOIN information_schema.key_column_usage kcu
        ON kcu.constraint_name = tc.constraint_name
       AND kcu.table_schema = tc.table_schema
     WHERE tc.table_schema = 'omninode_internal'
       AND tc.table_name = 'claude_hook_events'
       AND tc.constraint_type = 'PRIMARY KEY'
     GROUP BY tc.constraint_name
    HAVING count(*) = 1 AND bool_and(kcu.column_name = 'event_id')
  ) AS assertion;

SELECT 1 / count(*) AS claude_hook_events_not_null_columns_assertion
  FROM (
    SELECT 1
     WHERE (
       SELECT count(*)
         FROM information_schema.columns
        WHERE table_schema = 'omninode_internal'
          AND table_name = 'claude_hook_events'
          AND column_name IN (
                'event_id', 'session_id', 'is_subagent', 'hook_event_name',
                'correlation_id', 'emitted_at', 'payload', 'content_ref_ids',
                'source_topic', 'ingested_at', 'projection_cursor'
              )
          AND is_nullable = 'NO'
     ) = 11
  ) AS assertion;

SELECT 1 / count(*) AS claude_hook_events_session_id_is_a_column_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'session_id'
   AND data_type = 'text';

SELECT 1 / count(*) AS claude_hook_events_payload_is_jsonb_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'payload'
   AND data_type = 'jsonb';

SELECT 1 / count(*) AS claude_agent_spans_primary_key_assertion
  FROM (
    SELECT tc.constraint_name
      FROM information_schema.table_constraints tc
      JOIN information_schema.key_column_usage kcu
        ON kcu.constraint_name = tc.constraint_name
       AND kcu.table_schema = tc.table_schema
     WHERE tc.table_schema = 'omninode_internal'
       AND tc.table_name = 'claude_agent_spans'
       AND tc.constraint_type = 'PRIMARY KEY'
     GROUP BY tc.constraint_name
    HAVING count(*) = 2
       AND bool_or(kcu.column_name = 'session_id')
       AND bool_or(kcu.column_name = 'agent_id')
  ) AS assertion;

SELECT 1 / count(*) AS claude_agent_spans_not_null_columns_assertion
  FROM (
    SELECT 1
     WHERE (
       SELECT count(*)
         FROM information_schema.columns
        WHERE table_schema = 'omninode_internal'
          AND table_name = 'claude_agent_spans'
          AND column_name IN (
                'session_id', 'agent_id', 'parent_resolution', 'started_at',
                'tool_call_count', 'first_seen_at', 'updated_at',
                'projection_cursor'
              )
          AND is_nullable = 'NO'
     ) = 8
  ) AS assertion;

COMMENT ON TABLE omninode_internal.claude_hook_events IS
  'Every captured Claude Code hook event, metadata only, with session and agent '
  'lineage (OMN-19513). Written by node_projection_claude_hook_events from '
  'onex.evt.omniclaude.hook-event.v1. agent_id NULL means the main thread.';

COMMENT ON TABLE omninode_internal.claude_agent_spans IS
  'One row per (session, subagent), folded from claude_hook_events (OMN-19513). '
  'parent_resolution separates a main-thread parent from an unresolved one.';
