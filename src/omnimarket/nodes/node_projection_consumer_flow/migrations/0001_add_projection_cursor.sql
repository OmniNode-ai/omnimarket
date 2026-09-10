-- OMN-18043: make the bus-backed consumer-flow exposure paginable.
--
-- The composite business key remains the conflict key used by the writer.
-- projection_cursor is a separate database-assigned, unique monotonic value
-- used by the projection API for a stable page boundary.  It is intentionally
-- added as a follow-up migration so the already-merged 0000 migration remains
-- immutable and existing rows are preserved.

ALTER TABLE omninode_internal.consumer_flow_windows
    ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;

ALTER TABLE omninode_internal.consumer_flow_windows
    ALTER COLUMN projection_cursor SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_consumer_flow_windows_projection_cursor
    ON omninode_internal.consumer_flow_windows (projection_cursor);
