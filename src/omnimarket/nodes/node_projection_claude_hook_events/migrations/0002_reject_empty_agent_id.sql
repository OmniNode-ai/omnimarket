-- =============================================================================
-- MIGRATION: reject empty-string agent_id on claude_hook_events / claude_agent_spans
-- =============================================================================
-- Ticket: OMN-19513 (hostile-review follow-up on omnibase_infra#4169).
--
-- WHY THIS FILE EXISTS
--   0000's CHECK constraint on claude_hook_events,
--   ck_claude_hook_events_subagent_flag CHECK (is_subagent = (agent_id IS
--   NOT NULL)), reads an empty string as "not null" the same as any other
--   value, so it does not catch agent_id = ''. Model-layer min_length=1 on
--   ModelClaudeHookLineageWire.agent_id, ModelClaudeHookEventRow.agent_id and
--   ModelKnownParentToolCall.agent_id closes the same gap in the writer, but
--   the table is the durable truth surface (OmniNode deterministic truth
--   doctrine): a row must not depend only on an application-layer check that
--   a future producer, a direct INSERT, or a refactor could bypass.
--
--   claude_agent_spans.agent_id is already NOT NULL (it is half the primary
--   key), so the same gap exists there too: '' passes NOT NULL exactly like
--   any other non-null value.
--
-- WHY EVERY ASSERTION IS A BARE SELECT
--   Same as 0000: the application-database SQL gate (OMN-15361) rejects any
--   new DO $$ ... $$ block in a migration touching an application-topology
--   schema. Every assertion here is the statically provable
--   `SELECT 1 / count(*)` idiom.
--
-- IDEMPOTENCY
--   Postgres has no `ADD CONSTRAINT IF NOT EXISTS`, and a DO block to guard
--   one is exactly the dynamic SQL the OMN-15361 gate's
--   `_requires_dynamic_sql_rejection` rejects outright for this schema. Every
--   statement below is instead plain, static DDL: `DROP CONSTRAINT IF EXISTS`
--   (natively idempotent) immediately followed by an unconditional
--   `ADD CONSTRAINT`, so re-running this file drops and re-adds the same
--   check rather than erroring on a duplicate name. Both tables are brand new
--   (created by 0000, not yet merged to dev), so there is no legacy row this
--   could ever conflict with.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. claude_hook_events: agent_id, when present, is never empty.
-- -----------------------------------------------------------------------------
ALTER TABLE omninode_internal.claude_hook_events
    DROP CONSTRAINT IF EXISTS ck_claude_hook_events_agent_id_not_empty;

ALTER TABLE omninode_internal.claude_hook_events
    ADD CONSTRAINT ck_claude_hook_events_agent_id_not_empty
    CHECK (agent_id IS NULL OR agent_id <> '');

-- -----------------------------------------------------------------------------
-- 2. claude_agent_spans: agent_id is NOT NULL already; it must also be
--    non-empty, since it is half the table's primary key and the sole
--    discriminator of which agent a span belongs to.
-- -----------------------------------------------------------------------------
ALTER TABLE omninode_internal.claude_agent_spans
    DROP CONSTRAINT IF EXISTS ck_claude_agent_spans_agent_id_not_empty;

ALTER TABLE omninode_internal.claude_agent_spans
    ADD CONSTRAINT ck_claude_agent_spans_agent_id_not_empty
    CHECK (agent_id <> '');

-- -----------------------------------------------------------------------------
-- 3. Post-conditions. Named assertions for BOTH the new constraints and the
--    pre-existing is_subagent CHECK from 0000, which 0000 declares but never
--    asserts the existence of (hostile-reviewer non-blocking note on
--    omnibase_infra#4169: "no test for the is_subagent CHECK constraint").
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS ck_claude_hook_events_subagent_flag_exists_assertion
  FROM pg_catalog.pg_constraint c
  JOIN pg_catalog.pg_namespace n ON n.oid = c.connamespace
  JOIN pg_catalog.pg_class t ON t.oid = c.conrelid
 WHERE n.nspname = 'omninode_internal'
   AND t.relname = 'claude_hook_events'
   AND c.conname = 'ck_claude_hook_events_subagent_flag'
   AND c.contype = 'c'
   AND c.convalidated;

SELECT 1 / count(*) AS ck_claude_hook_events_agent_id_not_empty_exists_assertion
  FROM pg_catalog.pg_constraint c
  JOIN pg_catalog.pg_namespace n ON n.oid = c.connamespace
  JOIN pg_catalog.pg_class t ON t.oid = c.conrelid
 WHERE n.nspname = 'omninode_internal'
   AND t.relname = 'claude_hook_events'
   AND c.conname = 'ck_claude_hook_events_agent_id_not_empty'
   AND c.contype = 'c'
   AND c.convalidated;

SELECT 1 / count(*) AS ck_claude_agent_spans_agent_id_not_empty_exists_assertion
  FROM pg_catalog.pg_constraint c
  JOIN pg_catalog.pg_namespace n ON n.oid = c.connamespace
  JOIN pg_catalog.pg_class t ON t.oid = c.conrelid
 WHERE n.nspname = 'omninode_internal'
   AND t.relname = 'claude_agent_spans'
   AND c.conname = 'ck_claude_agent_spans_agent_id_not_empty'
   AND c.contype = 'c'
   AND c.convalidated;
