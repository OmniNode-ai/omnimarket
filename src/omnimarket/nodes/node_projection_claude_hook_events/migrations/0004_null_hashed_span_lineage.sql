-- =============================================================================
-- MIGRATION: null the hashed model, description and workflow_phase on claude_agent_spans
-- =============================================================================
-- Ticket: OMN-20010 (sidecar lineage metadata on subagent spans).
-- Version: 1.0.0
--
-- WHY THIS FILE EXISTS
--   While the emit drainer ran an omnimarket whose capture redaction had no
--   entry for lineage.agent_model and lineage.agent_description, its fail-closed
--   default hashed both, so spans were written with values of the form
--   'sha256:<64 hex>' in model, description and workflow_phase (lineage.workflow_phase
--   was unlisted too). A hash carries no information.
--   The writer keeps the first non-null value
--   (COALESCE(s.model, EXCLUDED.model)), so a hashed value would block the
--   plain value forever. Setting the hashed values to NULL lets the next event
--   for the same span fill them in.
--
-- IDEMPOTENCY AND SHAPE
--   Three static UPDATE statements, no DO block and no dynamic SQL (the
--   application-database SQL gate rejects both for this schema). A re-run
--   matches no row and changes nothing. Each assertion is a bare SELECT that
--   divides by zero, and so aborts the file under ON_ERROR_STOP, when any
--   hashed value remains.
-- =============================================================================

UPDATE omninode_internal.claude_agent_spans
   SET model = NULL
 WHERE model LIKE 'sha256:%';

UPDATE omninode_internal.claude_agent_spans
   SET description = NULL
 WHERE description LIKE 'sha256:%';

UPDATE omninode_internal.claude_agent_spans
   SET workflow_phase = NULL
 WHERE workflow_phase LIKE 'sha256:%';

SELECT 1 / (1 - LEAST(count(*), 1)) AS claude_agent_spans_no_hashed_model_assertion
  FROM omninode_internal.claude_agent_spans
 WHERE model LIKE 'sha256:%';

SELECT 1 / (1 - LEAST(count(*), 1)) AS claude_agent_spans_no_hashed_description_assertion
  FROM omninode_internal.claude_agent_spans
 WHERE description LIKE 'sha256:%';

SELECT 1 / (1 - LEAST(count(*), 1)) AS claude_agent_spans_no_hashed_workflow_phase_assertion
  FROM omninode_internal.claude_agent_spans
 WHERE workflow_phase LIKE 'sha256:%';
