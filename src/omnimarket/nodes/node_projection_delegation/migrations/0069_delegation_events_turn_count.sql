-- OMN-18534: delegation_events stores the turn count of each delegation.
--
-- turn_count is the terminal's attempts_count: the authoritative total of
-- inference calls the delegation made, including compliance repairs, same-tier
-- retries, escalated rungs and the terminal attempt. It is a typed column
-- written from the terminal, never derived at read time from attempt_history
-- or a log. A delegation refused or failed before any turn is written as 0.
--
-- The column is nullable with no default. NULL marks only a row written before
-- this migration, or by a producer that does not report the count; every new
-- terminal writes an integer. This metadata-only migration does not rewrite
-- existing rows.

BEGIN;

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS turn_count INTEGER;

COMMIT;
