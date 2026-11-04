-- =============================================================================
-- MIGRATION: deliver the omninode_runtime grants for the compaction A/B tables
-- =============================================================================
-- Owner:   omnimarket.nodes.node_projection_compaction_ab
-- No BIGSERIAL column is declared, so no sequence grant is needed. No DELETE:
-- a recorded boundary or build score is history.
-- =============================================================================

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE ON omninode_internal.compaction_ab_rows TO omninode_runtime;

GRANT SELECT, INSERT ON omninode_internal.compaction_ab_build_series TO omninode_runtime;
