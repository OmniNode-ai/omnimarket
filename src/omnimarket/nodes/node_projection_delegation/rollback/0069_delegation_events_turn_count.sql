-- OMN-18534: rollback for
-- nodes/node_projection_delegation/0069_delegation_events_turn_count.sql.
--
-- Drops the turn_count column added by 0069 and its values. Manual execution
-- only. rollback/ is not mounted to docker-entrypoint-initdb.d and no runner
-- reads it. It does not remove 0069's ledger row, so the node loop will not
-- re-apply 0069 unless that row is removed too. Run it as the role that ran
-- 0069.

BEGIN;

ALTER TABLE delegation_events
    DROP COLUMN IF EXISTS turn_count;

COMMIT;
