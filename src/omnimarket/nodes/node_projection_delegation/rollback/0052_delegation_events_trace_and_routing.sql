-- OMN-19448: rollback for
-- nodes/node_projection_delegation/0052_delegation_events_trace_and_routing.sql.
--
-- Drops the terminal trace and routing columns added by 0052 and their values.
-- Manual execution only. rollback/ is not mounted to docker-entrypoint-initdb.d
-- and no runner reads it. It does not remove 0052's ledger row, so the node
-- loop will not re-apply 0052 unless that row is removed too. Run it as the
-- role that ran 0052.

BEGIN;

ALTER TABLE delegation_events
    DROP COLUMN IF EXISTS trace_id,
    DROP COLUMN IF EXISTS routed_model,
    DROP COLUMN IF EXISTS answering_backend;

COMMIT;
