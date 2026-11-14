-- OMN-23776: tables of node_ticket_hand_edit_alarm_effect.
-- Write records: the newest ticket-contract-written event per ticket (the newer Linear updatedAt wins).
CREATE TABLE IF NOT EXISTS omninode_internal.ticket_hand_edit_write_records (
    ticket_id TEXT PRIMARY KEY,
    contract_fingerprint TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    linear_updated_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL
);

-- ---- BEGIN OMN-15376 shape reconciliation: ticket_hand_edit_write_records ----
-- Converges a drifted pre-existing table onto the declared shape; no-ops on fresh create. No DROP.
ALTER TABLE omninode_internal.ticket_hand_edit_write_records ADD COLUMN IF NOT EXISTS ticket_id TEXT;
ALTER TABLE omninode_internal.ticket_hand_edit_write_records ADD COLUMN IF NOT EXISTS contract_fingerprint TEXT;
ALTER TABLE omninode_internal.ticket_hand_edit_write_records ADD COLUMN IF NOT EXISTS content_sha256 TEXT;
ALTER TABLE omninode_internal.ticket_hand_edit_write_records ADD COLUMN IF NOT EXISTS linear_updated_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.ticket_hand_edit_write_records ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ;
-- ---- END OMN-15376 shape reconciliation: ticket_hand_edit_write_records ----

-- Cursor: the newest updatedAt the node has fully checked; one row.
CREATE TABLE IF NOT EXISTS omninode_internal.ticket_hand_edit_cursor (
    cursor_key TEXT PRIMARY KEY,
    updated_after TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL
);

-- ---- BEGIN OMN-15376 shape reconciliation: ticket_hand_edit_cursor ----
-- Converges a drifted pre-existing table onto the declared shape; no-ops on fresh create. No DROP.
ALTER TABLE omninode_internal.ticket_hand_edit_cursor ADD COLUMN IF NOT EXISTS cursor_key TEXT;
ALTER TABLE omninode_internal.ticket_hand_edit_cursor ADD COLUMN IF NOT EXISTS updated_after TIMESTAMPTZ;
ALTER TABLE omninode_internal.ticket_hand_edit_cursor ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ;
-- ---- END OMN-15376 shape reconciliation: ticket_hand_edit_cursor ----

-- Open findings: ticket_hand_edit and detector_unreadable, one row per finding key.
-- A finding closes only on a later fact: closed_at and closed_by name it.
CREATE TABLE IF NOT EXISTS omninode_internal.ticket_hand_edit_open_findings (
    finding_key TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    written_fingerprint TEXT,
    found_fingerprint TEXT,
    found_content_sha256 TEXT,
    differences TEXT,
    editors TEXT,
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    closed_by TEXT
);

-- ---- BEGIN OMN-15376 shape reconciliation: ticket_hand_edit_open_findings ----
-- Converges a drifted pre-existing table onto the declared shape; no-ops on fresh create. No DROP.
ALTER TABLE omninode_internal.ticket_hand_edit_open_findings ADD COLUMN IF NOT EXISTS finding_key TEXT;
ALTER TABLE omninode_internal.ticket_hand_edit_open_findings ADD COLUMN IF NOT EXISTS ticket_id TEXT;
ALTER TABLE omninode_internal.ticket_hand_edit_open_findings ADD COLUMN IF NOT EXISTS kind TEXT;
ALTER TABLE omninode_internal.ticket_hand_edit_open_findings ADD COLUMN IF NOT EXISTS written_fingerprint TEXT;
ALTER TABLE omninode_internal.ticket_hand_edit_open_findings ADD COLUMN IF NOT EXISTS found_fingerprint TEXT;
ALTER TABLE omninode_internal.ticket_hand_edit_open_findings ADD COLUMN IF NOT EXISTS found_content_sha256 TEXT;
ALTER TABLE omninode_internal.ticket_hand_edit_open_findings ADD COLUMN IF NOT EXISTS differences TEXT;
ALTER TABLE omninode_internal.ticket_hand_edit_open_findings ADD COLUMN IF NOT EXISTS editors TEXT;
ALTER TABLE omninode_internal.ticket_hand_edit_open_findings ADD COLUMN IF NOT EXISTS opened_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.ticket_hand_edit_open_findings ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.ticket_hand_edit_open_findings ADD COLUMN IF NOT EXISTS closed_by TEXT;
-- ---- END OMN-15376 shape reconciliation: ticket_hand_edit_open_findings ----
CREATE INDEX IF NOT EXISTS idx_ticket_hand_edit_open_findings_ticket
    ON omninode_internal.ticket_hand_edit_open_findings (ticket_id);
