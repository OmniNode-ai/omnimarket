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

CREATE INDEX IF NOT EXISTS idx_view_seam_readings_read_at
    ON public.view_seam_readings (read_at DESC);
