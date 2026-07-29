-- OMN-13839: node-owned projection migration for skill_execution_snapshots.
--
-- WHY THIS EXISTS
--   Completes the skill-measurement pipeline:
--     emit (OMN-13830) -> skill_executions table (omnibase_infra DB)
--       -> [this snapshot topic] -> skill-adoption widget (OMN-13832).
--
--   Before this node the projection API returned 404 unknown_topic for
--   onex.snapshot.projection.skill-executions.v1 — the read model the
--   omnidash skill-adoption widget consumes had no backing table and no
--   writer. node_projection_skill_executions declares projection_api over
--   skill_execution_snapshots (topic
--   onex.snapshot.projection.skill-executions.v1) and materializes rows by
--   folding the SAME skill-lifecycle bus events that populate the
--   omnibase_infra skill_executions table:
--     - onex.evt.omniclaude.skill-started.v1
--     - onex.evt.omniclaude.skill-completed.v1
--
--   DATA-PLANE NOTE (OMN-13839): the projection API binds one DSN and only
--   serves schemas in discovery.ALLOWED_SCHEMAS ({public, omnidash_analytics}).
--   The source skill_executions table lives in the omnibase_infra DB, which the
--   projection API does not read. Rather than reaching cross-DB, this node
--   follows node_projection_cost_by_repo exactly: it subscribes to the
--   skill-lifecycle EVENT topics on the bus and writes its own per-skill
--   aggregate into skill_execution_snapshots (public schema), the table the
--   projection API actually serves. Both materializations consume the same
--   events, so no cross-DB read is required.
--
--   Discovered + applied by scripts/run-projection-migrations.py (node-owned
--   migrations/ discovery) and vendored to the dashboard projection DB
--   (omnidash_analytics) the projection API binds to.
--
-- Idempotency: CREATE TABLE / INDEX / TRIGGER guarded so the migration is safe
-- on a DB where the table already exists and on a fresh omnidash_analytics.

-- ============================================================================
-- SKILL_EXECUTION_SNAPSHOTS TABLE
-- ============================================================================
-- One row per (skill_name, repo_id, window, minute) aggregate. Each inbound
-- skill-started / skill-completed event increments exactly one lifecycle
-- counter; the unique key accumulates counts additively across events.
-- ============================================================================
CREATE TABLE IF NOT EXISTS skill_execution_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    skill_name VARCHAR(256) NOT NULL,
    repo_id VARCHAR(256) NOT NULL,
    "window" VARCHAR(32) NOT NULL DEFAULT 'latest',
    snapshot_timestamp_minute TIMESTAMPTZ NOT NULL,

    -- Lifecycle counters (started vs completed) + completed status breakdown.
    started_count BIGINT NOT NULL DEFAULT 0,
    completed_count BIGINT NOT NULL DEFAULT 0,
    success_count BIGINT NOT NULL DEFAULT 0,
    failed_count BIGINT NOT NULL DEFAULT 0,
    partial_count BIGINT NOT NULL DEFAULT 0,

    -- Receipt coverage: fraction of started skills that produced a completed
    -- (receipt) event, clamped to [0, 1]. DB-computed from the stored counters
    -- so it is always consistent with the accumulated totals. Orphan completed
    -- events (no matching started) are clamped by LEAST(1.0, ...).
    receipt_coverage NUMERIC(5, 4) GENERATED ALWAYS AS (
        CASE
            WHEN started_count > 0
                THEN LEAST(1.0, completed_count::numeric / started_count)
            ELSE 0
        END
    ) STORED,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_skill_exec_skill_repo_window_minute
        UNIQUE (skill_name, repo_id, "window", snapshot_timestamp_minute),

    CONSTRAINT non_negative_skill_exec_started_count CHECK (started_count >= 0),
    CONSTRAINT non_negative_skill_exec_completed_count CHECK (completed_count >= 0),
    CONSTRAINT non_negative_skill_exec_success_count CHECK (success_count >= 0),
    CONSTRAINT non_negative_skill_exec_failed_count CHECK (failed_count >= 0),
    CONSTRAINT non_negative_skill_exec_partial_count CHECK (partial_count >= 0)
);

-- ---- BEGIN OMN-15376 shape reconciliation: skill_execution_snapshots ----
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

ALTER TABLE skill_execution_snapshots ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE skill_execution_snapshots ADD COLUMN IF NOT EXISTS skill_name VARCHAR(256);
ALTER TABLE skill_execution_snapshots ADD COLUMN IF NOT EXISTS repo_id VARCHAR(256);
ALTER TABLE skill_execution_snapshots ADD COLUMN IF NOT EXISTS "window" VARCHAR(32) DEFAULT 'latest';
ALTER TABLE skill_execution_snapshots ADD COLUMN IF NOT EXISTS snapshot_timestamp_minute TIMESTAMPTZ;
ALTER TABLE skill_execution_snapshots ADD COLUMN IF NOT EXISTS started_count BIGINT DEFAULT 0;
ALTER TABLE skill_execution_snapshots ADD COLUMN IF NOT EXISTS completed_count BIGINT DEFAULT 0;
ALTER TABLE skill_execution_snapshots ADD COLUMN IF NOT EXISTS success_count BIGINT DEFAULT 0;
ALTER TABLE skill_execution_snapshots ADD COLUMN IF NOT EXISTS failed_count BIGINT DEFAULT 0;
ALTER TABLE skill_execution_snapshots ADD COLUMN IF NOT EXISTS partial_count BIGINT DEFAULT 0;
ALTER TABLE skill_execution_snapshots ADD COLUMN IF NOT EXISTS receipt_coverage NUMERIC(5, 4) GENERATED ALWAYS AS ( CASE WHEN started_count > 0 THEN LEAST(1.0, completed_count::numeric / started_count) ELSE 0 END ) STORED;
ALTER TABLE skill_execution_snapshots ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE skill_execution_snapshots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'skill_name', 'repo_id', 'window', 'snapshot_timestamp_minute', 'started_count', 'completed_count', 'success_count', 'failed_count', 'partial_count', 'created_at', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'skill_execution_snapshots'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'skill_execution_snapshots'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge skill_execution_snapshots.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'skill_execution_snapshots'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE skill_execution_snapshots ADD CONSTRAINT skill_execution_snapshots_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'skill_execution_snapshots'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['repo_id', 'skill_name', 'snapshot_timestamp_minute', 'window']::text[]
    ) THEN
        ALTER TABLE skill_execution_snapshots ADD CONSTRAINT uq_skill_exec_skill_repo_window_minute UNIQUE (skill_name, repo_id, "window", snapshot_timestamp_minute);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'skill_execution_snapshots'::regclass AND conname = 'non_negative_skill_exec_started_count'
    ) THEN
        ALTER TABLE skill_execution_snapshots ADD CONSTRAINT non_negative_skill_exec_started_count CHECK (started_count >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'skill_execution_snapshots'::regclass AND conname = 'non_negative_skill_exec_completed_count'
    ) THEN
        ALTER TABLE skill_execution_snapshots ADD CONSTRAINT non_negative_skill_exec_completed_count CHECK (completed_count >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'skill_execution_snapshots'::regclass AND conname = 'non_negative_skill_exec_success_count'
    ) THEN
        ALTER TABLE skill_execution_snapshots ADD CONSTRAINT non_negative_skill_exec_success_count CHECK (success_count >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'skill_execution_snapshots'::regclass AND conname = 'non_negative_skill_exec_failed_count'
    ) THEN
        ALTER TABLE skill_execution_snapshots ADD CONSTRAINT non_negative_skill_exec_failed_count CHECK (failed_count >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'skill_execution_snapshots'::regclass AND conname = 'non_negative_skill_exec_partial_count'
    ) THEN
        ALTER TABLE skill_execution_snapshots ADD CONSTRAINT non_negative_skill_exec_partial_count CHECK (partial_count >= 0);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: skill_execution_snapshots ----


CREATE INDEX IF NOT EXISTS idx_skill_execution_snapshots_started_count
    ON skill_execution_snapshots (started_count DESC);

CREATE INDEX IF NOT EXISTS idx_skill_execution_snapshots_skill_name
    ON skill_execution_snapshots (skill_name);

CREATE INDEX IF NOT EXISTS idx_skill_execution_snapshots_window
    ON skill_execution_snapshots ("window");

CREATE INDEX IF NOT EXISTS idx_skill_execution_snapshots_updated_at
    ON skill_execution_snapshots (updated_at DESC);

-- ============================================================================
-- TRIGGER: auto-update updated_at
-- ============================================================================
CREATE OR REPLACE FUNCTION update_skill_execution_snapshots_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_skill_execution_snapshots_updated_at ON skill_execution_snapshots;
CREATE TRIGGER trigger_skill_execution_snapshots_updated_at
    BEFORE UPDATE ON skill_execution_snapshots
    FOR EACH ROW
    EXECUTE FUNCTION update_skill_execution_snapshots_updated_at();
