-- OMN-19448: delegation_events stores the requested model and terminal timings.
--
-- requested_model is the model_id of the first attempt on the delegate-skill
-- terminal's ladder (attempts[0]), or the canonical terminal's own
-- requested_model when it names one; routed_model stays the answering model.
-- queue_wait_ms and execution_ms are the terminal's queue_wait_ms and
-- execution_duration_ms, respectively.
--
-- All columns are nullable with no default. NULL means not measured, never
-- zero; a measured zero is stored as zero. This metadata-only migration does
-- not rewrite existing rows.

BEGIN;

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS requested_model TEXT,
    ADD COLUMN IF NOT EXISTS queue_wait_ms INTEGER,
    ADD COLUMN IF NOT EXISTS execution_ms INTEGER;

COMMIT;
