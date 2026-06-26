-- OMN-13321 / Enforcement Map F5 (hardens OMN-12569):
-- node-owned projection migration for pr_lifecycle_ledger_entries.
--
-- WHY THIS EXISTS
--   node_pr_lifecycle_state_reducer declares projection_api over
--   pr_lifecycle_ledger_entries (topic
--   onex.snapshot.projection.pr-lifecycle-ledger.v1). The state reducer UPSERTs
--   one user-readable ledger row per PR EVERY sweep iteration (not only at sweep
--   end), so an operator can answer "what does the ledger say right now?".
--
--   OMN-12569 landed a provenance-rich, run_id-keyed derived ledger but did not
--   materialize a clean per-iteration row in practice; F5 hardens that.
--
--   Vendored by omnibase_infra/scripts/sync-node-migrations.sh into
--   docker/migrations/forward/nodes/node_pr_lifecycle_state_reducer/ and applied
--   to NODE_POSTGRES_DB (omnidash_analytics) by run-forward-migrations.sh under
--   the namespaced migration id
--   node:node_pr_lifecycle_state_reducer:<file>.
--
-- KEY SHAPE
--   Conflict key (sweep_id, repo, pr_number, iteration): one row per PR per
--   iteration. Re-applying the same iteration's event is idempotent (UPSERT);
--   two consecutive iterations produce two distinct rows.
--
--   DoD probe:
--     select count(*) from pr_lifecycle_ledger_entries where sweep_id='<id>';
--   returns >= the open-PR count for that sweep.
--
-- Idempotency: CREATE TABLE / INDEX guarded by IF NOT EXISTS so the migration is
-- safe on a DB where the table already exists and on a fresh omnidash_analytics.

-- ============================================================================
-- PR_LIFECYCLE_LEDGER_ENTRIES TABLE
-- ============================================================================
-- One user-readable ledger row per PR per sweep iteration.
CREATE TABLE IF NOT EXISTS public.pr_lifecycle_ledger_entries (
    id                BIGSERIAL    PRIMARY KEY,
    sweep_id          TEXT         NOT NULL,
    iteration         INTEGER      NOT NULL,
    found_at          TIMESTAMPTZ  NOT NULL,
    repo              TEXT         NOT NULL,
    pr_number         INTEGER      NOT NULL,
    initial_state     TEXT         NOT NULL,
    action_taken      TEXT         NOT NULL,
    evidence          TEXT         NOT NULL DEFAULT '',
    final_state       TEXT         NOT NULL,
    next_check_at     TIMESTAMPTZ  NOT NULL,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (sweep_id, repo, pr_number, iteration)
);

-- Primary DoD query: count rows for a sweep.
CREATE INDEX IF NOT EXISTS idx_pr_lifecycle_ledger_sweep
    ON public.pr_lifecycle_ledger_entries (sweep_id);

-- Per-iteration ordering within a sweep.
CREATE INDEX IF NOT EXISTS idx_pr_lifecycle_ledger_sweep_iter
    ON public.pr_lifecycle_ledger_entries (sweep_id, iteration);

-- Projection-API read ordering (order_by found_at DESC).
CREATE INDEX IF NOT EXISTS idx_pr_lifecycle_ledger_found_at
    ON public.pr_lifecycle_ledger_entries (found_at DESC);
