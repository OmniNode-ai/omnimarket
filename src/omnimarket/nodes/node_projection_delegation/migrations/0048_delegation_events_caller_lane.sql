-- OMN-19860: delegation_events.caller_lane
--
-- Every delegation_events row carries the ledger lane that issued the
-- delegation, as its delegate-skill terminal carried it, stored in
-- caller_lane TEXT. Together with the existing session_id column this makes
-- per-lane delegation use queryable from the event stream, instead of being
-- inferred by joining ledger CLAIM and TERMINAL windows.
--
-- The column stays NULL on a row whose terminal carried no lane and on every
-- row written before this migration.
--
-- The change is additive and nullable: older writers never name the column
-- and keep working unchanged.

BEGIN;

ALTER TABLE delegation_events ADD COLUMN IF NOT EXISTS caller_lane TEXT;

CREATE INDEX IF NOT EXISTS idx_delegation_events_caller_lane ON delegation_events (caller_lane) WHERE caller_lane IS NOT NULL;

COMMIT;
