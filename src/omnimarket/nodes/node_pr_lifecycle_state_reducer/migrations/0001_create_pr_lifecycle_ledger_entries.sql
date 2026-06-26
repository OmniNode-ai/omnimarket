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

-- Warm-table reconciliation (OMN-13321 / CodeRabbit Major):
-- CREATE TABLE IF NOT EXISTS skips the entire body — including the inline
-- UNIQUE (sweep_id, repo, pr_number, iteration) — when the table already
-- exists. The reducer UPSERT relies on that conflict target, so on a warm
-- omnidash_analytics where the table was created without it (e.g. a prior
-- partial/hand-create), the ON CONFLICT would fail. Add the unique constraint
-- idempotently so the conflict target is guaranteed regardless of how the
-- table came to exist. (On a fresh DB the inline UNIQUE already created an
-- equivalent auto-named constraint; this DO block is a no-op there.)
DO $$
DECLARE
    target_cols  smallint[];
    has_unique   boolean;
BEGIN
    -- Resolve the attnums of the four conflict-key columns (set, sorted).
    SELECT array_agg(a.attnum ORDER BY a.attnum)
    INTO   target_cols
    FROM   pg_attribute a
    JOIN   pg_class t ON t.oid = a.attrelid
    JOIN   pg_namespace n ON n.oid = t.relnamespace
    WHERE  n.nspname = 'public'
      AND  t.relname = 'pr_lifecycle_ledger_entries'
      AND  a.attname IN ('sweep_id', 'repo', 'pr_number', 'iteration')
      AND  NOT a.attisdropped;

    -- Does any UNIQUE constraint already cover exactly those columns
    -- (order-independent — the inline UNIQUE auto-named constraint counts)?
    SELECT EXISTS (
        SELECT 1
        FROM   pg_constraint c
        JOIN   pg_class t ON t.oid = c.conrelid
        JOIN   pg_namespace n ON n.oid = t.relnamespace
        WHERE  n.nspname = 'public'
          AND  t.relname = 'pr_lifecycle_ledger_entries'
          AND  c.contype = 'u'
          AND  (SELECT array_agg(k ORDER BY k) FROM unnest(c.conkey) AS k) = target_cols
    )
    INTO   has_unique;

    IF NOT has_unique THEN
        ALTER TABLE public.pr_lifecycle_ledger_entries
            ADD CONSTRAINT uq_pr_lifecycle_ledger_sweep_repo_pr_iter
            UNIQUE (sweep_id, repo, pr_number, iteration);
    END IF;
END
$$;
