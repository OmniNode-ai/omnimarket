-- =============================================================================
-- MIGRATION: claude_hook_events guard, refusal_reason and token usage
-- =============================================================================
-- Ticket: OMN-18946 (a refused call names its guard; an event carries token
--         use).
-- Contract: omniclaude src/omniclaude/hooks/contracts/contract_hook_claude_capture.yaml,
--           section `projection.event_table`.
-- Version: 1.0.0
--
-- WHY THIS FILE EXISTS
--   Two facts had no column, because no event stated them:
--     guard, refusal_reason   3,939 refused calls in one week carried no guard
--                             name. The guard lived only in the rate-limited
--                             ledger FRICTION rows, which join to no event. A
--                             refused PreToolUse has no PostToolUse, so the
--                             refusal was visible only as an absence. The
--                             producer now derives one GuardRefusal event per
--                             refused call; its row carries the guard and the
--                             guard's normalised reason class, and its
--                             tool_use_id is the refused call's, so
--                               SELECT pre.*, r.guard, r.refusal_reason
--                                 FROM claude_hook_events pre
--                                 JOIN claude_hook_events r
--                                   ON r.session_id = pre.session_id
--                                  AND r.tool_use_id = pre.tool_use_id
--                                  AND r.hook_event_name = 'GuardRefusal'
--                                WHERE pre.hook_event_name = 'PreToolUse'
--                             names the guard on the call.
--     input_tokens ...        No event carried tokens, usage or price. The
--     assistant_message_count producer now derives one AssistantUsage event per
--                             model per Stop, SubagentStop or SessionEnd that
--                             finds new assistant messages in the transcript.
--                             A session's total is
--                               SELECT sum(input_tokens), sum(output_tokens),
--                                      sum(cache_creation_input_tokens),
--                                      sum(cache_read_input_tokens)
--                                 FROM claude_hook_events
--                                WHERE session_id = $1
--                                  AND hook_event_name = 'AssistantUsage'
--                             across the main thread and every subagent (they
--                             share the session id).
--   Every column is NULL on every event that does not state it. `model`,
--   already a column (0006), is the API's model id on an AssistantUsage row.
--
-- WHY BIGINT
--   A session summed in SQL, and a single long-running agent, outgrow INTEGER
--   long before a column of per-event sums does; the cells are BIGINT so a
--   reader never has to cast.
--
-- IDEMPOTENCY AND SHAPE
--   Every DDL statement is guarded for replay, so a re-run is a no-op. Bare
--   SELECT 1 / count(*) assertions reject a column that exists with the wrong
--   shape: division by zero makes ON_ERROR_STOP abort the file. The
--   table-level grants in 0001 already cover the new columns; no grants are
--   repeated.
-- =============================================================================

ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS guard TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS refusal_reason TEXT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS input_tokens BIGINT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS output_tokens BIGINT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS cache_creation_input_tokens BIGINT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS cache_read_input_tokens BIGINT;
ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS assistant_message_count INTEGER;

-- A refusal row is found by its guard; a usage row by its session. Both are a
-- small fraction of the table, so both indexes are partial.
CREATE INDEX IF NOT EXISTS idx_claude_hook_events_guard_refusal
    ON omninode_internal.claude_hook_events (guard, emitted_at DESC)
    WHERE hook_event_name = 'GuardRefusal';

-- The refused call's join: (session, call) for the refusal rows, beside the
-- PreToolUse partial index of 0000.
CREATE INDEX IF NOT EXISTS idx_claude_hook_events_refusal_call
    ON omninode_internal.claude_hook_events (session_id, tool_use_id)
    WHERE hook_event_name = 'GuardRefusal';

CREATE INDEX IF NOT EXISTS idx_claude_hook_events_assistant_usage
    ON omninode_internal.claude_hook_events (session_id, emitted_at)
    WHERE hook_event_name = 'AssistantUsage';

SELECT 1 / count(*) AS claude_hook_events_guard_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'guard'
   AND data_type = 'text'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS claude_hook_events_refusal_reason_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'refusal_reason'
   AND data_type = 'text'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS claude_hook_events_input_tokens_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'input_tokens'
   AND data_type = 'bigint'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS claude_hook_events_output_tokens_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'output_tokens'
   AND data_type = 'bigint'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS claude_hook_events_cache_creation_input_tokens_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'cache_creation_input_tokens'
   AND data_type = 'bigint'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS claude_hook_events_cache_read_input_tokens_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'cache_read_input_tokens'
   AND data_type = 'bigint'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS claude_hook_events_assistant_message_count_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'assistant_message_count'
   AND data_type = 'integer'
   AND is_nullable = 'YES';
