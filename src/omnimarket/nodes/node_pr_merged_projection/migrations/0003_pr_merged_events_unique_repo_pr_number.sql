-- OMN-19998: one pr_merged_events row per merged PR, keyed on (repo, pr_number).
--
-- 0001 deduped on event_id, the publisher-minted UUID. A merge that reaches the
-- topic twice under two event_ids (a webhook redelivery, a mirror republish, the
-- publisher workflow rerun) therefore produced two rows, and the worktree reaper
-- saw the same PR twice at two different cursors. HandlerPrMergedProjection now
-- upserts on (repo, pr_number); this migration gives that ON CONFLICT target a
-- unique key to name.
--
-- Existing duplicates are collapsed first, keeping the row with the lowest
-- projection_cursor: that row is the one a reaper polling ?since=<cursor> has
-- already read or will read first, so no consumer's cursor moves backwards and
-- no merge disappears from the feed.
--
-- Idempotency: the DELETE is a no-op once the key exists, and the constraint is
-- added only when no unique constraint on exactly (pr_number, repo) is present.

DELETE FROM pr_merged_events newer
USING pr_merged_events older
WHERE newer.repo = older.repo
  AND newer.pr_number = older.pr_number
  AND newer.projection_cursor > older.projection_cursor;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint c
        WHERE c.conrelid = 'pr_merged_events'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_catalog.pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['pr_number', 'repo']::text[]
    ) THEN
        ALTER TABLE pr_merged_events
            ADD CONSTRAINT pr_merged_events_repo_pr_number_key UNIQUE (repo, pr_number);
    END IF;
END$$;
