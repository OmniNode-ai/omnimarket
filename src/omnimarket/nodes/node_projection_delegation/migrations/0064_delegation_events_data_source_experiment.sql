-- OMN-19448: typed synthetic delegation traffic is experiment data.
-- Replace the closed check without scanning existing real/fixture rows.

BEGIN;

ALTER TABLE delegation_events
    DROP CONSTRAINT IF EXISTS delegation_events_data_source_check;

ALTER TABLE delegation_events
    ADD CONSTRAINT delegation_events_data_source_check
    CHECK (data_source IN ('real', 'fixture', 'experiment')) NOT VALID;

COMMIT;
