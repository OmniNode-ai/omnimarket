-- OMN-22020: node-owned projection migration for view_seam_readings.
--
-- node_view_seam_reading_reducer declares projection_api over view_seam_readings
-- (topic onex.snapshot.projection.view-seam-readings.v1). The reducer is the only
-- writer; the dashboard only reads. One row per (seam, window); a replay replaces
-- its logical row.
--
-- Idempotency: guarded by IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS public.view_seam_readings (
    seam_id      TEXT        NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end   TIMESTAMPTZ NOT NULL,
    state        TEXT        NOT NULL,
    crossings    INTEGER     NOT NULL DEFAULT 0,
    mismatched   INTEGER     NOT NULL DEFAULT 0,
    unreadable   INTEGER     NOT NULL DEFAULT 0,
    examples     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    read_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (seam_id, window_start)
);

-- ---- BEGIN OMN-15376 shape reconciliation: view_seam_readings ----
-- CREATE TABLE IF NOT EXISTS above no-ops against a pre-existing table of a
-- different shape, and the index below names a column, so a drifted table would
-- stop the whole migration. These guarded adds converge it onto the declared
-- shape; on a fresh create every one is a no-op. No DROP, no recreate, no
-- TRUNCATE. Gated by tests/ci/test_node_migration_shape_reconciliation.py.
ALTER TABLE public.view_seam_readings ADD COLUMN IF NOT EXISTS seam_id TEXT;
ALTER TABLE public.view_seam_readings ADD COLUMN IF NOT EXISTS window_start TIMESTAMPTZ;
ALTER TABLE public.view_seam_readings ADD COLUMN IF NOT EXISTS window_end TIMESTAMPTZ;
ALTER TABLE public.view_seam_readings ADD COLUMN IF NOT EXISTS state TEXT;
ALTER TABLE public.view_seam_readings ADD COLUMN IF NOT EXISTS crossings INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.view_seam_readings ADD COLUMN IF NOT EXISTS mismatched INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.view_seam_readings ADD COLUMN IF NOT EXISTS unreadable INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.view_seam_readings ADD COLUMN IF NOT EXISTS examples JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE public.view_seam_readings ADD COLUMN IF NOT EXISTS read_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.view_seam_readings'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.view_seam_readings
            ADD CONSTRAINT view_seam_readings_pkey PRIMARY KEY (seam_id, window_start);
    END IF;
END$$;

-- A column that holds NULL in a pre-existing row makes these statements fail loud
-- and name the column; the migration never invents a value.
ALTER TABLE public.view_seam_readings ALTER COLUMN window_end SET NOT NULL;
ALTER TABLE public.view_seam_readings ALTER COLUMN state SET NOT NULL;
ALTER TABLE public.view_seam_readings ALTER COLUMN read_at SET NOT NULL;
-- ---- END OMN-15376 shape reconciliation: view_seam_readings ----

CREATE INDEX IF NOT EXISTS idx_view_seam_readings_read_at
    ON public.view_seam_readings (read_at DESC);
