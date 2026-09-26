-- =============================================================================
-- MIGRATION: omninode_internal.session_content -- full session content
-- =============================================================================
-- Ticket: OMN-19550. Parent epic: OMN-16176.
-- Version: 1.0.0
--
-- WHY THIS FILE EXISTS
--   Operator ruling 2026-09-25 (RULING row 2026-09-25T11:23:30Z): full content
--   is captured through hooks, so full context joins to deterministic outcomes
--   as labelled training and eval data. Hook capture was metadata only;
--   omninode_internal.work_events holds a tool name and a duration per call.
--   This table holds the content itself: the complete prompt, tool input, tool
--   result and assistant reply, one row per content chunk, after the
--   capture-redaction contract scrubbed credential-shaped spans at publish.
--
-- JOINS
--   session_id and correlation_id join to omninode_internal.work_events
--   (actor_id, payload->>'correlation_id'); turn_id joins every row of one
--   turn; tool_use_id pairs a tool input with its result. A content item split
--   into chunks reassembles by (session_id, turn_id, tool_use_id,
--   content_kind) ordered by chunk_index, verified against content_sha256.
--
-- RETENTION
--   Kept. The runtime role gets no DELETE, and nothing here prunes. Pruning is
--   the archive plan's step and follows only a verified archive (RULING row
--   2026-09-25T11:20:55Z); idx_session_content_emitted_at is the window it
--   would use.
--
-- The precondition idiom, the shape reconciliation block and the grant-last
-- ordering follow node_projection_work_events/migrations/0001 verbatim; see
-- that file for why each is written the way it is (no DO blocks under the
-- application-database SQL gate, ADD COLUMN IF NOT EXISTS for OMN-15376,
-- post-conditions before any GRANT).
-- =============================================================================

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

SELECT 1 / count(*) AS omninode_runtime_role_exists_precondition
  FROM pg_catalog.pg_roles
 WHERE rolname = 'omninode_runtime';

CREATE TABLE IF NOT EXISTS omninode_internal.session_content (
  event_id            TEXT        PRIMARY KEY,
  session_id          TEXT        NOT NULL,
  turn_id             TEXT,
  correlation_id      TEXT,
  tool_use_id         TEXT,
  tool_name           TEXT,
  content_kind        TEXT        NOT NULL,
  chunk_index         INTEGER     NOT NULL DEFAULT 0,
  chunk_count         INTEGER     NOT NULL DEFAULT 1,
  content             TEXT        NOT NULL DEFAULT '',
  command             JSONB,
  content_sha256      TEXT,
  original_chars      INTEGER,
  truncated           BOOLEAN     NOT NULL DEFAULT FALSE,
  redaction_state     TEXT,
  producer_redaction  JSONB       NOT NULL DEFAULT '{}',
  hook_source         TEXT,
  emitted_at          TIMESTAMPTZ NOT NULL,
  source_topic        TEXT        NOT NULL DEFAULT '',
  ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: omninode_internal.session_content ----
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS event_id TEXT;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS turn_id TEXT;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS tool_use_id TEXT;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS tool_name TEXT;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS content_kind TEXT;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS chunk_index INTEGER DEFAULT 0;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS chunk_count INTEGER DEFAULT 1;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS content TEXT DEFAULT '';
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS command JSONB;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS content_sha256 TEXT;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS original_chars INTEGER;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS truncated BOOLEAN DEFAULT FALSE;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS redaction_state TEXT;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS producer_redaction JSONB DEFAULT '{}';
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS hook_source TEXT;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS emitted_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS source_topic TEXT DEFAULT '';
ALTER TABLE omninode_internal.session_content
  ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ DEFAULT NOW();
-- ---- END OMN-15376 shape reconciliation: omninode_internal.session_content ----

CREATE INDEX IF NOT EXISTS idx_session_content_session
  ON omninode_internal.session_content (session_id, emitted_at);

CREATE INDEX IF NOT EXISTS idx_session_content_turn
  ON omninode_internal.session_content (turn_id)
  WHERE turn_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_session_content_tool_use
  ON omninode_internal.session_content (tool_use_id)
  WHERE tool_use_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_session_content_emitted_at
  ON omninode_internal.session_content (emitted_at DESC);

-- Post-conditions, BEFORE any GRANT (see work_events 0001 section 6).
SELECT 1 / count(*) AS session_content_exists_assertion
  FROM information_schema.tables
 WHERE table_schema = 'omninode_internal' AND table_name = 'session_content';

SELECT 1 / count(*) AS session_content_primary_key_assertion
  FROM (
    SELECT tc.constraint_name
      FROM information_schema.table_constraints tc
      JOIN information_schema.key_column_usage kcu
        ON kcu.constraint_name = tc.constraint_name
       AND kcu.table_schema = tc.table_schema
     WHERE tc.table_schema = 'omninode_internal'
       AND tc.table_name = 'session_content'
       AND tc.constraint_type = 'PRIMARY KEY'
     GROUP BY tc.constraint_name
    HAVING count(*) = 1 AND bool_and(kcu.column_name = 'event_id')
  ) AS assertion;

SELECT 1 / count(*) AS session_content_not_null_columns_assertion
  FROM (
    SELECT 1
     WHERE (
       SELECT count(*)
         FROM information_schema.columns
        WHERE table_schema = 'omninode_internal'
          AND table_name = 'session_content'
          AND column_name IN (
                'event_id', 'session_id', 'content_kind', 'chunk_index',
                'chunk_count', 'content', 'truncated', 'producer_redaction',
                'emitted_at', 'source_topic', 'ingested_at'
              )
          AND is_nullable = 'NO'
     ) = 11
  ) AS assertion;

-- Runtime-owns-DB: the projection writer's scope, and no DELETE.
GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;
GRANT SELECT, INSERT, UPDATE ON omninode_internal.session_content TO omninode_runtime;

COMMENT ON TABLE omninode_internal.session_content IS
  'Full session content (OMN-19550): prompt, tool input, tool result and '
  'assistant reply per chunk, scrubbed by the capture-redaction contract, '
  'projected by node_projection_session_content. Kept; pruned only after a '
  'verified archive.';

SELECT 1 / count(*) AS omninode_runtime_session_content_insert_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'session_content'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'INSERT';
