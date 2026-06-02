-- OMN-12606: stamp materializer provenance on delegation_events rows.
--
-- The reducer/orchestrator completion path materializes a fresh terminal
-- delegation event directly into delegation_events (no operator backfill,
-- May 31 finding). Each materialized row records the projection schema
-- contract version and the reducer logic version so the proof packet can
-- attribute a fresh row to a known materializer (OMN-12488 acceptance-extension).

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS projection_version TEXT NOT NULL DEFAULT '1.0.0',
    ADD COLUMN IF NOT EXISTS reducer_version TEXT NOT NULL DEFAULT '1.0.0';
