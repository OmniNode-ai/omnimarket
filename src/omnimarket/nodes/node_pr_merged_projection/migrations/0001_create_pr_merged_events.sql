-- OMN-13227 / T3: Create the pr_merged_events projection table.
--
-- HandlerPrMergedProjection UPSERTs one row per onex.evt.github.pr-merged.v1
-- event, deduped by event_id (the publisher-minted UUID). The per-machine
-- worktree reaper (OMN-13228 / T4) polls
--   GET /projection/onex.evt.github.pr-merged.v1?since=<cursor>
-- and matches {repo, branch, pr_number, ticket} to a local worktree, then runs
-- prune-worktrees.sh against it.
--
-- projection_cursor is a strictly-monotonic BIGSERIAL: the generic projection
-- API filters rows with projection_cursor > :since and returns the largest
-- value as next_cursor, so the reaper never re-processes a merged PR.

CREATE TABLE IF NOT EXISTS pr_merged_events (
    projection_cursor BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    ticket TEXT NOT NULL DEFAULT '',
    merged_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: pr_merged_events ----
-- The CREATE TABLE IF NOT EXISTS above SILENTLY NO-OPS when a table of this
-- name already exists with a DIFFERENT shape (an out-of-band or legacy apply
-- that predates this migration). Everything below it in this file is NOT so
-- forgiving: CREATE INDEX IF NOT EXISTS guards the index NAME, not the COLUMN,
-- so the first column-dependent statement raises
--   ERROR: column "<col>" does not exist
-- and ON_ERROR_STOP=1 kills the whole migration Job there. Because the runner
-- halts at the first failure, instances of this class surface strictly one per
-- deploy cycle -- OMN-15376 (llm_cost_aggregates.aggregation_key, run
-- 30418878385) and OMN-15302 (baselines_comparisons.snapshot_id) each cost one.
--
-- The guarded adds below converge a drifted pre-existing table onto the shape
-- declared above. On the fresh-create path every one is a no-op (the column
-- already exists), so BOTH paths end at the same schema. No DROP, no recreate,
-- no TRUNCATE: pre-existing rows are preserved. A column that cannot be made
-- NOT NULL without inventing data fails LOUD and names the exact conflict
-- instead of guessing.
--
-- Gated by tests/ci/test_node_migration_shape_reconciliation.py (static) and
-- tests/integration/migrations/test_node_migration_shape_drift_omn15376.py
-- (RED/GREEN + fresh-vs-drifted schema equality on real Postgres).

ALTER TABLE pr_merged_events ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;
ALTER TABLE pr_merged_events ADD COLUMN IF NOT EXISTS event_id TEXT;
ALTER TABLE pr_merged_events ADD COLUMN IF NOT EXISTS repo TEXT;
ALTER TABLE pr_merged_events ADD COLUMN IF NOT EXISTS branch TEXT;
ALTER TABLE pr_merged_events ADD COLUMN IF NOT EXISTS pr_number INTEGER;
ALTER TABLE pr_merged_events ADD COLUMN IF NOT EXISTS ticket TEXT DEFAULT '';
ALTER TABLE pr_merged_events ADD COLUMN IF NOT EXISTS merged_at TIMESTAMPTZ;
ALTER TABLE pr_merged_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['projection_cursor', 'event_id', 'repo', 'branch', 'pr_number', 'ticket', 'merged_at', 'created_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'pr_merged_events'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'pr_merged_events'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge pr_merged_events.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'pr_merged_events'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE pr_merged_events ADD CONSTRAINT pr_merged_events_pkey PRIMARY KEY (projection_cursor);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'pr_merged_events'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['event_id']::text[]
    ) THEN
        ALTER TABLE pr_merged_events ADD CONSTRAINT pr_merged_events_event_id_key UNIQUE (event_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: pr_merged_events ----


CREATE INDEX IF NOT EXISTS idx_pr_merged_events_cursor
    ON pr_merged_events (projection_cursor);

CREATE INDEX IF NOT EXISTS idx_pr_merged_events_repo_branch
    ON pr_merged_events (repo, branch);

CREATE INDEX IF NOT EXISTS idx_pr_merged_events_merged_at
    ON pr_merged_events (merged_at DESC);
