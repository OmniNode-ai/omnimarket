-- =============================================================================
-- MIGRATION: dod_verify_runs names the delegation run a verification judged
-- =============================================================================
-- Ticket:  OMN-19514 (decision-workflow eval plan, Task 4)
-- Owner:   omnimarket.nodes.node_projection_dod_verdict
-- Version: 1.0.0
--
-- WHY THIS EXISTS
--   correlation_id on this table identifies the VERIFICATION run. It says
--   nothing about which delegated attempt produced the work being verified,
--   so no verdict row could be joined to a delegation_events row, and a
--   delegated attempt could not be judged against its ticket's definition of
--   done. The verify start command now accepts the delegation's correlation
--   id, the verdict carries it, and this column stores it.
--
-- SHAPE
--   Nullable and additive. NULL on every row written before this migration
--   and on every verification that judged no delegated attempt. Older writers
--   never name the column and keep working. The table's existing grants cover
--   the new column; no sequence is added.
--
-- THE JOIN
--   delegation_events.correlation_id = dod_verify_runs.delegation_correlation_id
--   The partial index serves that join from this side.
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS delegation_correlation_id UUID;

CREATE INDEX IF NOT EXISTS idx_dod_verify_runs_delegation_correlation_id
    ON omninode_internal.dod_verify_runs (delegation_correlation_id)
    WHERE delegation_correlation_id IS NOT NULL;
