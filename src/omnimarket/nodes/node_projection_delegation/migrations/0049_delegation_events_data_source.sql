-- OMN-19970: delegation_events.data_source
--
-- Every delegation_events row names its provenance: 'real' for a row a real
-- delegation wrote, 'fixture' for a row the dev and demo seed (`onex seed`,
-- node_dev_seed_effect) wrote through the same projection so dashboard pages
-- have rows before the real chain is complete. Measured sums exclude
-- 'fixture' rows unless a reader opts in (migration 0050 for the summary view).
--
-- NOT NULL DEFAULT 'real' is a metadata-only change on PostgreSQL 11+: every
-- existing row reads 'real', which is what it is, and older writers that never
-- name the column keep working unchanged. The CHECK is added NOT VALID so the
-- migration takes no table scan; existing rows all hold the default, and every
-- new row is checked.

BEGIN;

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS data_source TEXT NOT NULL DEFAULT 'real';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'delegation_events_data_source_check'
          AND conrelid = 'delegation_events'::regclass
    ) THEN
        ALTER TABLE delegation_events
            ADD CONSTRAINT delegation_events_data_source_check
            CHECK (data_source IN ('real', 'fixture')) NOT VALID;
    END IF;
END$$;

COMMIT;
