-- =============================================================================
-- MIGRATION: the overlay store
-- =============================================================================
-- Ticket: OMN-22478. The overlay home is the source; the projection holds each loaded
-- overlay here, one row per overlay id and document digest: every version ever loaded
-- and when it was first and last asserted. The current version of an overlay is its row
-- asserted last. The document is JSON and not JSONB: JSONB reorders keys, and a table
-- file's key order is part of how the flat parsers read it.
-- =============================================================================

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

-- Shape reconciliation: guarded ADDs precede the index; a drifted table converges on these columns.
ALTER TABLE omninode_internal.overlay_store
    ADD COLUMN IF NOT EXISTS overlay_id      TEXT;
ALTER TABLE omninode_internal.overlay_store
    ADD COLUMN IF NOT EXISTS digest          TEXT;
ALTER TABLE omninode_internal.overlay_store
    ADD COLUMN IF NOT EXISTS document        JSON;
ALTER TABLE omninode_internal.overlay_store
    ADD COLUMN IF NOT EXISTS source_ref      TEXT;
ALTER TABLE omninode_internal.overlay_store
    ADD COLUMN IF NOT EXISTS loaded_by       TEXT;
ALTER TABLE omninode_internal.overlay_store
    ADD COLUMN IF NOT EXISTS first_loaded_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.overlay_store
    ADD COLUMN IF NOT EXISTS last_loaded_at  TIMESTAMPTZ;
ALTER TABLE omninode_internal.overlay_store
    ADD COLUMN IF NOT EXISTS projected_at    TIMESTAMPTZ DEFAULT now();

CREATE INDEX IF NOT EXISTS overlay_store_current_idx
    ON omninode_internal.overlay_store (overlay_id, last_loaded_at DESC);
