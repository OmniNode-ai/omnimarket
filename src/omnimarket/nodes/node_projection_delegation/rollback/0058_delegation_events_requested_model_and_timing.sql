-- OMN-19448: rollback for
-- nodes/node_projection_delegation/0058_delegation_events_requested_model_and_timing.sql.
--
-- Drops the requested_model, queue_wait_ms and execution_ms columns added by
-- 0058 and their values. Manual execution only. rollback/ is not mounted to
-- docker-entrypoint-initdb.d and no runner reads it. It does not remove 0058's
-- ledger row, so the node loop will not re-apply 0058 unless that row is removed
-- too. Run it as the role that ran 0058.

BEGIN;

ALTER TABLE delegation_events
    DROP COLUMN IF EXISTS requested_model,
    DROP COLUMN IF EXISTS queue_wait_ms,
    DROP COLUMN IF EXISTS execution_ms;

COMMIT;
