-- =============================================================================
-- MIGRATION: the overlay store's revision, provenance, write ids and audit rows
-- =============================================================================
-- Ticket: OMN-26635. A set reaches the store with the revision it was built on, the paths an operator set and its
-- audit row. The store applies it only when that revision is the overlay's current one, and writes the record, its
-- provenance, its write id and its audit row in one transaction. Counts and digests only: no table holds a secret.
-- =============================================================================

-- The version rows carry the revision the store numbered them with. The paths an operator set are the JSON column
-- operator_paths that 0002_add_operator_paths.sql adds.
ALTER TABLE omninode_internal.overlay_store
    ADD COLUMN IF NOT EXISTS revision       INTEGER NOT NULL DEFAULT 0;

-- One row per overlay: the revision its current version holds. A write is checked against this row, under the
-- overlay's advisory lock.
CREATE TABLE IF NOT EXISTS omninode_internal.overlay_store_revision (
    overlay_id TEXT PRIMARY KEY,
    revision   INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE omninode_internal.overlay_store_revision ADD COLUMN IF NOT EXISTS overlay_id TEXT;
ALTER TABLE omninode_internal.overlay_store_revision ADD COLUMN IF NOT EXISTS revision INTEGER;
ALTER TABLE omninode_internal.overlay_store_revision ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

-- One row per applied write id: the revision it was committed at, so a delivery of the same id is answered, not applied.
CREATE TABLE IF NOT EXISTS omninode_internal.overlay_store_write (
    write_id           TEXT PRIMARY KEY,
    overlay_id         TEXT NOT NULL,
    revision           INTEGER NOT NULL,
    based_on_revision  INTEGER NOT NULL,
    applied_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE omninode_internal.overlay_store_write ADD COLUMN IF NOT EXISTS write_id TEXT;
ALTER TABLE omninode_internal.overlay_store_write ADD COLUMN IF NOT EXISTS overlay_id TEXT;
ALTER TABLE omninode_internal.overlay_store_write ADD COLUMN IF NOT EXISTS revision INTEGER;
ALTER TABLE omninode_internal.overlay_store_write ADD COLUMN IF NOT EXISTS based_on_revision INTEGER;
ALTER TABLE omninode_internal.overlay_store_write ADD COLUMN IF NOT EXISTS applied_at TIMESTAMPTZ DEFAULT now();

-- Who set the value at each path an operator set, in which revision, under which write id. A path with no row holds
-- the default of the record's source_ref.
CREATE TABLE IF NOT EXISTS omninode_internal.overlay_store_provenance (
    overlay_id TEXT NOT NULL,
    path       TEXT NOT NULL,
    source     TEXT NOT NULL,
    actor      TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    write_id   TEXT NOT NULL,
    PRIMARY KEY (overlay_id, path)
);
ALTER TABLE omninode_internal.overlay_store_provenance ADD COLUMN IF NOT EXISTS overlay_id TEXT;
ALTER TABLE omninode_internal.overlay_store_provenance ADD COLUMN IF NOT EXISTS path TEXT;
ALTER TABLE omninode_internal.overlay_store_provenance ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE omninode_internal.overlay_store_provenance ADD COLUMN IF NOT EXISTS actor TEXT;
ALTER TABLE omninode_internal.overlay_store_provenance ADD COLUMN IF NOT EXISTS revision INTEGER;
ALTER TABLE omninode_internal.overlay_store_provenance ADD COLUMN IF NOT EXISTS write_id TEXT;

-- One audit row per write id, written in the transaction of the record. published_at is set once the audit topic carries
-- the row, so a delivery after a failed publish publishes it and no other delivery does.
CREATE TABLE IF NOT EXISTS omninode_internal.overlay_store_audit (
    write_id     TEXT PRIMARY KEY,
    overlay_id   TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    key          TEXT NOT NULL,
    setting_path TEXT NOT NULL,
    actor        TEXT NOT NULL,
    audit        JSON NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);
ALTER TABLE omninode_internal.overlay_store_audit ADD COLUMN IF NOT EXISTS write_id TEXT;
ALTER TABLE omninode_internal.overlay_store_audit ADD COLUMN IF NOT EXISTS overlay_id TEXT;
ALTER TABLE omninode_internal.overlay_store_audit ADD COLUMN IF NOT EXISTS revision INTEGER;
ALTER TABLE omninode_internal.overlay_store_audit ADD COLUMN IF NOT EXISTS key TEXT;
ALTER TABLE omninode_internal.overlay_store_audit ADD COLUMN IF NOT EXISTS setting_path TEXT;
ALTER TABLE omninode_internal.overlay_store_audit ADD COLUMN IF NOT EXISTS actor TEXT;
ALTER TABLE omninode_internal.overlay_store_audit ADD COLUMN IF NOT EXISTS audit JSON;
ALTER TABLE omninode_internal.overlay_store_audit ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE omninode_internal.overlay_store_audit ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS overlay_store_current_revision_idx
    ON omninode_internal.overlay_store (overlay_id, revision DESC, last_loaded_at DESC);
CREATE INDEX IF NOT EXISTS overlay_store_audit_overlay_idx
    ON omninode_internal.overlay_store_audit (overlay_id, revision);
