-- OMN-18534: delegation_events stores the turn count of each delegation.
--
-- turn_count is the terminal's attempts_count: the authoritative total of
-- inference calls the delegation made, including compliance repairs, same-tier
-- retries, escalated rungs and the terminal attempt. It is a typed column
-- written only from the count a terminal reported, and never derived from
-- attempt_history, a log or any other column, at write or at read time. A
-- delegation refused before dispatch, or one that made no inference call and
-- reported no count, is written as 0.
--
-- The column is nullable with no default. NULL marks a row written before this
-- migration and a terminal whose producer reported no count: a routed terminal
-- with no count stores NULL, and so does a failure that carries a malformed
-- count or evidence of an attempt. This metadata-only migration does not
-- rewrite existing rows.

BEGIN;

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS turn_count INTEGER;

COMMIT;
