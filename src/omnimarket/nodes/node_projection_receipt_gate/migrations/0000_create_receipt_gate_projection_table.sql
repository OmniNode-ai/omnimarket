-- Migration: 0000_create_receipt_gate_projection_table.sql
-- Node: node_projection_receipt_gate
-- Ticket: OMN-13081
-- Creates receipt_gate_rows table for the receipt-gate projection API.
-- This table backs the projection API endpoint for
-- onex.snapshot.projection.receipt-gate.v1, consumed by the omnidash
-- receipt-gate widget.

CREATE TABLE IF NOT EXISTS receipt_gate_rows (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    pass BOOLEAN NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    pr_ref TEXT,
    worker TEXT,
    verifier TEXT,
    evidence_count INTEGER,
    evidence_hash TEXT,
    signed_at TEXT,
    observed_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS receipt_gate_rows_observed_at_idx
    ON receipt_gate_rows (observed_at DESC);

CREATE INDEX IF NOT EXISTS receipt_gate_rows_name_idx
    ON receipt_gate_rows (name);

CREATE INDEX IF NOT EXISTS receipt_gate_rows_pass_idx
    ON receipt_gate_rows (pass);
