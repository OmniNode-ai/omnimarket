-- OMN-19448: delegation_events terminal trace and routing columns.
--
-- The canonical delegation-completed/failed terminal (omnibase_core
-- ModelDelegationResult) supplies trace_id from trace_id, routed_model from
-- model_used, and answering_backend from route. Store the wire UUID trace_id
-- as TEXT so the asyncpg writer can bind its string representation safely.
--
-- All three columns are nullable with no default: older terminals may omit
-- them. This metadata-only migration does not rewrite existing rows.

BEGIN;

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS trace_id TEXT,
    ADD COLUMN IF NOT EXISTS routed_model TEXT,
    ADD COLUMN IF NOT EXISTS answering_backend TEXT;

COMMIT;
