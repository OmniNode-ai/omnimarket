-- OMN-19448: delegation_events terminal stop reason and truncation columns.
--
-- Both columns are nullable with no default: older terminals may omit the
-- stop reason. This metadata-only migration does not rewrite existing rows.

BEGIN;

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS finish_reason TEXT,
    ADD COLUMN IF NOT EXISTS truncated BOOLEAN;

COMMIT;
