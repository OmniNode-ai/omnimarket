-- OMN-14648 / WS6: Create the merge_state_transitions projection table.
--
-- HandlerMergeStateProjection UPSERTs one row per
-- onex.evt.omnimarket.merge-state-transition.v1 event, deduped by event_id (the
-- deterministic 16-hex fingerprint of the transition's identifying tuple). The
-- merge-flow metrics (merge_state_metrics_native.compute_merge_flow_metrics) are
-- materialized from these rows: per-state duration, evidence-volume ratio
-- (baseline 1.67 -> target <=1.1), companions per product PR, same-head reruns
-- by reason code, queue wait, and product failures before vs after evidence.
--
-- projection_cursor is a strictly-monotonic BIGSERIAL: the generic projection
-- API filters rows with projection_cursor > :since and returns the largest
-- value as next_cursor, so a reader never re-processes a transition.
--
-- REPORT-ONLY: no enforcement / WIP cap is wired off this table in this PR.

CREATE TABLE IF NOT EXISTS merge_state_transitions (
    projection_cursor BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    branch TEXT NOT NULL DEFAULT '',
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    reason_code TEXT,
    is_occ_evidence BOOLEAN NOT NULL DEFAULT FALSE,
    product_pr_number INTEGER,
    queue_wait_seconds DOUBLE PRECISION,
    product_failure_found BOOLEAN NOT NULL DEFAULT FALSE,
    evidence_present BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: merge_state_transitions ----
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

ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;
ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS event_id TEXT;
ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS repo TEXT;
ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS pr_number INTEGER;
ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS head_sha TEXT;
ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS branch TEXT DEFAULT '';
ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS from_state TEXT;
ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS to_state TEXT;
ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMPTZ;
ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS reason_code TEXT;
ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS is_occ_evidence BOOLEAN DEFAULT FALSE;
ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS product_pr_number INTEGER;
ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS queue_wait_seconds DOUBLE PRECISION;
ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS product_failure_found BOOLEAN DEFAULT FALSE;
ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS evidence_present BOOLEAN DEFAULT FALSE;
ALTER TABLE merge_state_transitions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['projection_cursor', 'event_id', 'repo', 'pr_number', 'head_sha', 'branch', 'from_state', 'to_state', 'occurred_at', 'is_occ_evidence', 'product_failure_found', 'evidence_present', 'created_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'merge_state_transitions'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'merge_state_transitions'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge merge_state_transitions.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'merge_state_transitions'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE merge_state_transitions ADD CONSTRAINT merge_state_transitions_pkey PRIMARY KEY (projection_cursor);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'merge_state_transitions'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['event_id']::text[]
    ) THEN
        ALTER TABLE merge_state_transitions ADD CONSTRAINT merge_state_transitions_event_id_key UNIQUE (event_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: merge_state_transitions ----


CREATE INDEX IF NOT EXISTS idx_merge_state_transitions_cursor
    ON merge_state_transitions (projection_cursor);

CREATE INDEX IF NOT EXISTS idx_merge_state_transitions_repo_pr
    ON merge_state_transitions (repo, pr_number);

CREATE INDEX IF NOT EXISTS idx_merge_state_transitions_occurred_at
    ON merge_state_transitions (occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_merge_state_transitions_reason
    ON merge_state_transitions (reason_code);
