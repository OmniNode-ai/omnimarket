-- OMN-18930 (K3 of OMN-18925): every delegation_events row carries the
-- delegation cohort key its terminal carried -- every dimension that must be
-- equal before two runs' outcomes are compared -- so a comparison is made
-- between rows, and two rows differ only where their keys differ.
--
--   cohort_key          the complete key as the terminal carried it (JSONB)
--   cohort_key_sha256   SHA-256 of that key, sorted members, compact separators
--   cohort_key_refusal  why a key the terminal carried was refused (incomplete
--                       or malformed); the key and its digest are then NULL
--
-- All three stay NULL on a row whose terminal carried no key, and on every row
-- written before this migration. Additive and nullable: older writers never
-- name these columns and keep working.
BEGIN;

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS cohort_key JSONB,
    ADD COLUMN IF NOT EXISTS cohort_key_sha256 TEXT,
    ADD COLUMN IF NOT EXISTS cohort_key_refusal TEXT;

COMMIT;
