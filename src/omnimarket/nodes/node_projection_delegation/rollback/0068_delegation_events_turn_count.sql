-- OMN-18534: rollback for
-- nodes/node_projection_delegation/0068_delegation_events_turn_count.sql.
--
-- Drops the turn_count column added by 0068 and its values. Manual execution
-- only. rollback/ is not mounted to docker-entrypoint-initdb.d and no runner
-- reads it. It does not remove 0068's ledger row, so the node loop will not
-- re-apply 0068 unless that row is removed too. Run it as the role that ran
-- 0068.

BEGIN;

ALTER TABLE delegation_events
    DROP COLUMN IF EXISTS turn_count;

COMMIT;
