-- =============================================================================
-- MIGRATION: the overlay store
-- =============================================================================
-- Ticket: OMN-22478. The overlay home is the source; the projection holds each loaded
-- overlay here, one row per overlay id and document digest: every version ever loaded
-- and when it was first and last asserted. The current version of an overlay is its row
-- asserted last. The document is JSON and not JSONB: JSONB reorders keys, and a table
-- file's key order is part of how the flat parsers read it.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS omninode_internal;

CREATE TABLE IF NOT EXISTS omninode_internal.overlay_store (
    overlay_id      TEXT NOT NULL,
    digest          TEXT NOT NULL,
    document        JSON NOT NULL,
    source_ref      TEXT NOT NULL,
    loaded_by       TEXT NOT NULL,
    first_loaded_at TIMESTAMPTZ NOT NULL,
    last_loaded_at  TIMESTAMPTZ NOT NULL,
    projected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (overlay_id, digest)
);

CREATE INDEX IF NOT EXISTS overlay_store_current_idx
    ON omninode_internal.overlay_store (overlay_id, last_loaded_at DESC);
