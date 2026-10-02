-- =============================================================================
-- MIGRATION: claude_hook_events lane, model, host and exit_code
-- =============================================================================
-- Ticket: OMN-17427 (hook events must say which lane, model and machine ran a
--         call, and how a Bash call exited).
-- Contract: omniclaude src/omniclaude/hooks/contracts/contract_hook_claude_capture.yaml,
--           section `projection.event_table`.
-- Version: 1.0.0
--
-- WHY THIS FILE EXISTS
--   A probe of the .201 dev lane (2026-10-01) found none of these four facts
--   in a column: there was no column for any of them. Each is promoted by the
--   fold from what the event already carries:
--     lane       the emit seam's top-level `lane` stamp, which the consumer
--                used to drop by name; an empty stamp is stored as NULL.
--     model      `lineage.agent_model` (the harness sidecar) on a subagent's
--                event, else `payload.model` on SessionStart.
--     host       the event's `host`, the emitting machine's hostname.
--     exit_code  `payload.exit_code` on a Bash PostToolUse or
--                PostToolUseFailure event.
--   Every other event leaves the column NULL.
--
-- IDEMPOTENCY AND SHAPE
--   Every DDL statement is guarded for replay, so a re-run is a no-op. Bare
--   SELECT 1 / count(*) assertions reject a column that exists with the wrong
--   shape: division by zero makes ON_ERROR_STOP abort the file. The
--   table-level grants in 0001 already cover the new columns; no grants are
--   repeated.
-- =============================================================================

ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS lane TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS host TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS exit_code INTEGER;

SELECT 1 / count(*) AS claude_hook_events_lane_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'lane'
   AND data_type = 'text'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS claude_hook_events_model_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'model'
   AND data_type = 'text'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS claude_hook_events_host_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'host'
   AND data_type = 'text'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS claude_hook_events_exit_code_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'exit_code'
   AND data_type = 'integer'
   AND is_nullable = 'YES';
