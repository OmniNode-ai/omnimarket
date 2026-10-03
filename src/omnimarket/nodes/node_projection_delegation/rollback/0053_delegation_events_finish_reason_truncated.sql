-- OMN-19448: rollback for
-- nodes/node_projection_delegation/0053_delegation_events_finish_reason_truncated.sql.
--
-- Drops the terminal stop reason and truncation columns added by 0053 and their values.
-- Manual execution only. rollback/ is not mounted to docker-entrypoint-initdb.d
-- and no runner reads it. It does not remove 0053's ledger row, so the node
-- loop will not re-apply 0053 unless that row is removed too. Run it as the
-- role that ran 0053.

BEGIN;

ALTER TABLE delegation_events
    DROP COLUMN IF EXISTS finish_reason,
    DROP COLUMN IF EXISTS truncated;

COMMIT;
