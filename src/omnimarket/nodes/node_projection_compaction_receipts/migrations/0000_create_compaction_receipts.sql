-- =============================================================================
-- MIGRATION: durable context compaction receipts
-- =============================================================================
-- Owner:   omnimarket.nodes.node_projection_compaction_receipts
-- Version: 1.0.0
--
-- One row per session/boundary, plus activation-keyed capture-gap facts.
-- Decided verdicts are immutable; fuller captures can resolve uncertain rows.
-- The schema is provisioned by infrastructure, as in the alert-channel template.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.compaction_receipts (
    session_id                 TEXT NOT NULL,
    receipt_key                TEXT NOT NULL,
    boundary_uuid              TEXT,
    boundary_kind              TEXT,
    activation_id              UUID,
    verdict                    TEXT NOT NULL,
    reason                     TEXT,
    installed_messages_hash    TEXT,
    observed_hash              TEXT,
    source_topic               TEXT NOT NULL DEFAULT '',
    projection_cursor          BIGSERIAL NOT NULL,
    projected_at               TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (session_id, receipt_key)
);

-- Shape reconciliation: guarded ADDs precede indexes, as in the template.
-- Existing rows cannot satisfy new NOT NULL columns without defaults.
ALTER TABLE omninode_internal.compaction_receipts
    ADD COLUMN IF NOT EXISTS session_id                 TEXT;
ALTER TABLE omninode_internal.compaction_receipts
    ADD COLUMN IF NOT EXISTS receipt_key                TEXT;
ALTER TABLE omninode_internal.compaction_receipts
    ADD COLUMN IF NOT EXISTS boundary_uuid              TEXT;
ALTER TABLE omninode_internal.compaction_receipts
    ADD COLUMN IF NOT EXISTS boundary_kind              TEXT;
ALTER TABLE omninode_internal.compaction_receipts
    ADD COLUMN IF NOT EXISTS activation_id              UUID;
ALTER TABLE omninode_internal.compaction_receipts
    ADD COLUMN IF NOT EXISTS verdict                    TEXT;
ALTER TABLE omninode_internal.compaction_receipts
    ADD COLUMN IF NOT EXISTS reason                     TEXT;
ALTER TABLE omninode_internal.compaction_receipts
    ADD COLUMN IF NOT EXISTS installed_messages_hash    TEXT;
ALTER TABLE omninode_internal.compaction_receipts
    ADD COLUMN IF NOT EXISTS observed_hash              TEXT;
ALTER TABLE omninode_internal.compaction_receipts
    ADD COLUMN IF NOT EXISTS source_topic               TEXT DEFAULT '';
ALTER TABLE omninode_internal.compaction_receipts
    ADD COLUMN IF NOT EXISTS projection_cursor          BIGSERIAL;
ALTER TABLE omninode_internal.compaction_receipts
    ADD COLUMN IF NOT EXISTS projected_at               TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS idx_compaction_receipts_cursor
    ON omninode_internal.compaction_receipts (projection_cursor);

CREATE INDEX IF NOT EXISTS idx_compaction_receipts_session_verdict
    ON omninode_internal.compaction_receipts (session_id, verdict);
