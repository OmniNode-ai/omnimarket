-- =============================================================================
-- MIGRATION: claude_hook_events goal_id and parent_goal_id
-- =============================================================================
-- Ticket: OMN-20031 (goal contract GC.8: the goal stamp on the Bash capture).
-- Contract: omniclaude src/omniclaude/hooks/contracts/contract_hook_claude_capture.yaml,
--           section `projection.event_table`.
-- Version: 1.0.0
--
-- WHY THIS FILE EXISTS
--   The producer stamps `goal_id` (and `parent_goal_id` when there is a parent)
--   on the one PreToolUse Bash event whose command begins ONEX_GOAL=<uuid>.
--   Both ids are promoted from the payload to nullable uuid columns so a goal
--   can be found without scanning JSONB. Every other event leaves them NULL.
--   Assigning LATER events to a goal is a separate fold and is not done here.
--
-- IDEMPOTENCY AND SHAPE
--   Every DDL statement is guarded for replay, so a re-run is a no-op. Bare SELECT 1 / count(*) assertions reject a column or index that
--   exists with the wrong shape: division by zero makes ON_ERROR_STOP abort the
--   file. The table-level grants in 0001 already cover the new columns; no
--   grants are repeated.
-- =============================================================================

ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS goal_id UUID;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS parent_goal_id UUID;

CREATE INDEX IF NOT EXISTS idx_claude_hook_events_goal_id
    ON omninode_internal.claude_hook_events (goal_id)
    WHERE goal_id IS NOT NULL;

SELECT 1 / count(*) AS claude_hook_events_goal_id_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'goal_id'
   AND data_type = 'uuid'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS claude_hook_events_parent_goal_id_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'parent_goal_id'
   AND data_type = 'uuid'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS claude_hook_events_goal_id_index_assertion
  FROM pg_catalog.pg_indexes
 WHERE schemaname = 'omninode_internal'
   AND tablename = 'claude_hook_events'
   AND indexname = 'idx_claude_hook_events_goal_id'
   AND indexdef LIKE '%(goal_id)%goal_id IS NOT NULL%';
