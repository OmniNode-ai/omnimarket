-- OMN-20162: rollback for
-- nodes/node_projection_delegation/0054_delegation_events_backend_id_and_host.sql.
--
-- Drops the backend_id and host columns added by 0054 and their values.
-- Manual execution only. rollback/ is not mounted to docker-entrypoint-initdb.d
-- and no runner reads it. It does not remove 0054's ledger row, so the node
-- loop will not re-apply 0054 unless that row is removed too. Run it as the
-- role that ran 0054.

BEGIN;

ALTER TABLE delegation_events
    DROP COLUMN IF EXISTS backend_id,
    DROP COLUMN IF EXISTS host;

COMMIT;
