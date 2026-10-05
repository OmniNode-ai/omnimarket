-- Cross-run lineage carried by delegate-skill terminals.
-- The columns stay NULL on rows whose terminals carried no lineage and on
-- rows written before this migration. Older writers omit the columns.

BEGIN;

ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS parent_correlation_id TEXT;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS attempt_kind TEXT;
ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS parent_failure_cause TEXT;

CREATE INDEX IF NOT EXISTS idx_delegation_events_parent_correlation_id ON delegation_events (parent_correlation_id) WHERE parent_correlation_id IS NOT NULL;

COMMIT;
