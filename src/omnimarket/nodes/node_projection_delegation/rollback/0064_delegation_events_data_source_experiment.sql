-- OMN-19448: rollback fails if experiment rows exist: remove or reclassify
-- them before rollback so subsequent writes satisfy the restored check.
-- NOT VALID avoids scanning the table; existing rows are not validated here.
--
-- Manual execution only. rollback/ is not mounted to docker-entrypoint-initdb.d
-- and no runner reads it. It does not remove 0064's ledger row, so the node
-- loop will not re-apply 0064 unless that row is removed too. Run it as the
-- role that ran 0064.

BEGIN;

ALTER TABLE delegation_events
    DROP CONSTRAINT IF EXISTS delegation_events_data_source_check;

ALTER TABLE delegation_events
    ADD CONSTRAINT delegation_events_data_source_check
    CHECK (data_source IN ('real', 'fixture')) NOT VALID;

COMMIT;
