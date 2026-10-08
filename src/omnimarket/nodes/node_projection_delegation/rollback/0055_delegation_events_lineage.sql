-- OMN-20606: rollback for
-- nodes/node_projection_delegation/0055_delegation_events_lineage.sql.
--
-- Drops the lineage index and columns added by 0055 and their values.
-- Manual execution only. rollback/ is not mounted to docker-entrypoint-initdb.d
-- and no runner reads it. It does not remove 0055's ledger row, so the node
-- loop will not re-apply 0055 unless that row is removed too. Run it as the
-- role that ran 0055.

BEGIN;

DROP INDEX IF EXISTS idx_delegation_events_parent_correlation_id;

ALTER TABLE delegation_events
    DROP COLUMN IF EXISTS parent_correlation_id,
    DROP COLUMN IF EXISTS lineage_kind,
    DROP COLUMN IF EXISTS parent_failure_cause;

COMMIT;
