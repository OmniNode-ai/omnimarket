-- OMN-20606: delegation_events names the delegation a fallback or escalation follows.
--
-- A caller that retries failed work issues a new delegation with its own
-- correlation id. These columns let that row name the delegation it follows:
--
--   parent_correlation_id  the parent delegation's correlation id, canonical
--                          UUID text, joinable to delegation_events.correlation_id
--   lineage_kind           'fallback' (another route) or 'escalation' (a
--                          stronger model on the same route)
--   parent_failure_cause   a short token naming why the parent did not answer
--
-- All three are nullable with no default: rows written before this migration,
-- and delegations that follow nothing, read NULL. The writer names either the
-- whole lineage or none of it (handler_delegation_lineage_fold.py). The partial
-- index serves the join from a parent to the rows that follow it. This
-- metadata-only migration does not rewrite existing rows.

BEGIN;

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS parent_correlation_id TEXT,
    ADD COLUMN IF NOT EXISTS lineage_kind TEXT,
    ADD COLUMN IF NOT EXISTS parent_failure_cause TEXT;

CREATE INDEX IF NOT EXISTS idx_delegation_events_parent_correlation_id
    ON delegation_events (parent_correlation_id)
    WHERE parent_correlation_id IS NOT NULL;

COMMIT;
