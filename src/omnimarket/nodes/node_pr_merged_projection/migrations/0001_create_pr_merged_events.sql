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

CREATE INDEX IF NOT EXISTS idx_pr_merged_events_cursor
    ON pr_merged_events (projection_cursor);

CREATE INDEX IF NOT EXISTS idx_pr_merged_events_repo_branch
    ON pr_merged_events (repo, branch);

CREATE INDEX IF NOT EXISTS idx_pr_merged_events_merged_at
    ON pr_merged_events (merged_at DESC);
