-- =============================================================================
-- MIGRATION: index window_start for the live read and for retention
-- =============================================================================
-- Owner:   omnimarket.nodes.node_projection_consumer_flow
--
-- WHAT WAS SLOW
--   The board's consumer-flow live read selects the groups with a window in
--   the last 10 minutes:
--
--     SELECT DISTINCT consumer_group
--       FROM omninode_internal.consumer_flow_windows
--      WHERE window_start > now() - interval '10 minutes'
--
--   No index leads with window_start (the primary key leads with
--   consumer_group), so the plan is a Parallel Seq Scan over the whole heap.
--   Pruning does not shrink the heap file: measured on the lab dev lane
--   2026-10-16 after retention cut the relation from 47.7 million to 12.6
--   million rows, the read still scanned 1,496,869 heap blocks (11 GB).
--   node_retention_cleanup's age predicate is the same column.
--
-- WHY CONCURRENTLY / IF NOT EXISTS
--   As 0005: the relation is read live, a plain build holds ACCESS EXCLUSIVE,
--   and the forward-migration runner executes these files statement-wise.
-- =============================================================================

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_consumer_flow_windows_window_start
    ON omninode_internal.consumer_flow_windows (window_start);
