-- OMN-21970: an integer walk cursor on delegation_events.
--
-- The decisions exposure served only its newest 500 rows: its only unique
-- columns are a UUID id and correlation_id text, and the projection read can
-- start an ascending cursor walk only from an integer cursor (the walk origin is
-- one below the smallest value). So a reader could never page past the newest
-- 500 runs, and the Overview's per-run views showed about a day and a half.
--
-- projection_cursor is that integer cursor. BIGSERIAL numbers every existing
-- row when the column is added, so there is no separate backfill, and every
-- later insert takes the next value from its own sequence. A row rewritten in
-- place keeps its number: the dashboard walks the whole set on each read, so a
-- stable number visits each run exactly once.
--
-- The SQLite counterpart is a column set to the store's integer id, filled on
-- insert by a trigger (SqliteDatabaseAdapter store step
-- omn21970_delegation_events_cursor).

BEGIN;

ALTER TABLE public.delegation_events ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;
ALTER TABLE public.delegation_events ALTER COLUMN projection_cursor SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_delegation_events_projection_cursor
    ON public.delegation_events (projection_cursor);

-- The writer inserts rows, so it takes nextval() from the column's sequence.
GRANT USAGE ON SEQUENCE public.delegation_events_projection_cursor_seq
    TO tenant_projection_writer;

COMMIT;
