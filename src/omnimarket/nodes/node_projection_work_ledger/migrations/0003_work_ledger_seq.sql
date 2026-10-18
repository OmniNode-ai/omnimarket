-- =============================================================================
-- MIGRATION: omninode_internal.work_ledger_rows ledger sequence
-- =============================================================================
-- Ticket:  OMN-20541 (work-ledger cutover slice C9)
-- Owner:   omnimarket.nodes.node_projection_work_ledger
--
-- The judge assigns a gapless ledger_seq under the ledger lock. Persist it for
-- a database gap check that can replace the markdown-file parity witness.
-- The index is deliberately NOT unique: a unique violation in the projection
-- consumer would be a poison message, stalling every later ledger row on the
-- dev lane. The gap check reports duplicate sequences as a failure instead.
-- Nothing is backfilled here: existing rows keep NULL until a separate, ruled
-- history backfill supplies their sequence.
-- =============================================================================

ALTER TABLE omninode_internal.work_ledger_rows ADD COLUMN IF NOT EXISTS ledger_seq BIGINT;
CREATE INDEX IF NOT EXISTS idx_work_ledger_rows_seq
    ON omninode_internal.work_ledger_rows (ledger_id, ledger_seq)
    WHERE ledger_seq IS NOT NULL;
