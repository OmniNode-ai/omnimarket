-- =============================================================================
-- MIGRATION: claude_agent_spans model, description and workflow_phase
-- =============================================================================
-- Ticket: OMN-20010 (sidecar lineage metadata on subagent spans).
-- Contract: omniclaude src/omniclaude/hooks/contracts/contract_hook_claude_capture.yaml,
--           section `projection.lineage_table`.
-- Version: 1.0.0
--
-- WHY THIS FILE EXISTS
--   Preserve the sidecar's model, description and workflow phase on each span.
--   Description is a scrubbed 200-char task label, never a prompt. All three
--   columns are nullable because older producers omit these lineage fields.
--
-- IDEMPOTENCY AND SHAPE
--   ADD COLUMN IF NOT EXISTS is a no-op on replay. Bare SELECT 1 / count(*)
--   assertions reject existing columns with the wrong type or nullability:
--   division by zero makes ON_ERROR_STOP abort the file.
--   The SELECT, INSERT and UPDATE grants in 0001 are table-level and already
--   cover these new columns; no grants need to be repeated.
-- =============================================================================

ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE omninode_internal.claude_agent_spans
    ADD COLUMN IF NOT EXISTS workflow_phase TEXT;

SELECT 1 / count(*) AS claude_agent_spans_model_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_agent_spans'
   AND column_name = 'model'
   AND data_type = 'text'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS claude_agent_spans_description_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_agent_spans'
   AND column_name = 'description'
   AND data_type = 'text'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS claude_agent_spans_workflow_phase_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_agent_spans'
   AND column_name = 'workflow_phase'
   AND data_type = 'text'
   AND is_nullable = 'YES';
