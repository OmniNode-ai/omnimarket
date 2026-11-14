-- OMN-23776: tables of node_ticket_hand_edit_alarm_effect.
-- Written: the ticket node's newest write (or adoption) per ticket; the newer Linear updatedAt wins.
CREATE TABLE IF NOT EXISTS omninode_internal.ticket_hand_edit_written (
    ticket_id TEXT PRIMARY KEY,
    contract_fingerprint TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    kind TEXT NOT NULL,
    correlation_id TEXT NOT NULL
);

-- Cursor: the newest Linear updatedAt the node has read in full.
CREATE TABLE IF NOT EXISTS omninode_internal.ticket_hand_edit_cursor (
    name TEXT PRIMARY KEY,
    position TIMESTAMPTZ NOT NULL
);

-- Open findings: ticket_hand_edit and detector_unreadable, one row per finding key; the board reads it.
CREATE TABLE IF NOT EXISTS omninode_internal.ticket_hand_edit_open_findings (
    idempotency_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    fingerprint_written TEXT NOT NULL,
    fingerprint_found TEXT NOT NULL,
    content_sha256_found TEXT NOT NULL,
    differs TEXT NOT NULL,
    editors TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ticket_hand_edit_open_findings_ticket
    ON omninode_internal.ticket_hand_edit_open_findings (ticket_id);
