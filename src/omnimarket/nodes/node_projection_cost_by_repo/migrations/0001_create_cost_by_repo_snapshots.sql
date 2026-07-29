-- OMN-13077: node-owned projection migration for cost_by_repo_snapshots.
--
-- WHY THIS EXISTS
--   node_projection_cost_by_repo declares projection_api over
--   cost_by_repo_snapshots (topic onex.snapshot.projection.cost.by_repo.v1).
--   The dashboard cost-by-repo widget's projectionSchema requires the columns
--   repo_name, total_cost_usd, and window. The shared llm_cost_aggregates table
--   has no repo_name column, so the cost-by-repo widget was upstream-blocked.
--   This node-owned table gives the snapshot topic a dedicated backing table
--   that carries the repo dimension, removing the upstream block.
--
--   Discovered + applied by scripts/run-projection-migrations.py (node-owned
--   migrations/ discovery) and vendored to the dashboard projection DB
--   (omnidash_analytics) the projection API binds to.
--
-- Idempotency: CREATE TABLE / INDEX / TRIGGER guarded so the migration is safe
-- on a DB where the table already exists and on a fresh omnidash_analytics.

-- ============================================================================
-- COST_BY_REPO_SNAPSHOTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS cost_by_repo_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    repo_name VARCHAR(512) NOT NULL,
    "window" VARCHAR(32) NOT NULL DEFAULT 'latest',
    snapshot_timestamp_minute TIMESTAMPTZ NOT NULL,

    total_cost_usd NUMERIC(14, 6) NOT NULL DEFAULT 0,
    total_tokens BIGINT NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_cost_by_repo_repo_window_minute
        UNIQUE (repo_name, "window", snapshot_timestamp_minute),

    CONSTRAINT non_negative_cost_by_repo_total_cost_usd CHECK (total_cost_usd >= 0),
    CONSTRAINT non_negative_cost_by_repo_total_tokens CHECK (total_tokens >= 0)
);

-- ---- BEGIN OMN-15376 shape reconciliation: cost_by_repo_snapshots ----
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

ALTER TABLE cost_by_repo_snapshots ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE cost_by_repo_snapshots ADD COLUMN IF NOT EXISTS repo_name VARCHAR(512);
ALTER TABLE cost_by_repo_snapshots ADD COLUMN IF NOT EXISTS "window" VARCHAR(32) DEFAULT 'latest';
ALTER TABLE cost_by_repo_snapshots ADD COLUMN IF NOT EXISTS snapshot_timestamp_minute TIMESTAMPTZ;
ALTER TABLE cost_by_repo_snapshots ADD COLUMN IF NOT EXISTS total_cost_usd NUMERIC(14, 6) DEFAULT 0;
ALTER TABLE cost_by_repo_snapshots ADD COLUMN IF NOT EXISTS total_tokens BIGINT DEFAULT 0;
ALTER TABLE cost_by_repo_snapshots ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE cost_by_repo_snapshots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'repo_name', 'window', 'snapshot_timestamp_minute', 'total_cost_usd', 'total_tokens', 'created_at', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'cost_by_repo_snapshots'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'cost_by_repo_snapshots'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge cost_by_repo_snapshots.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'cost_by_repo_snapshots'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE cost_by_repo_snapshots ADD CONSTRAINT cost_by_repo_snapshots_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'cost_by_repo_snapshots'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['repo_name', 'snapshot_timestamp_minute', 'window']::text[]
    ) THEN
        ALTER TABLE cost_by_repo_snapshots ADD CONSTRAINT uq_cost_by_repo_repo_window_minute UNIQUE (repo_name, "window", snapshot_timestamp_minute);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'cost_by_repo_snapshots'::regclass AND conname = 'non_negative_cost_by_repo_total_cost_usd'
    ) THEN
        ALTER TABLE cost_by_repo_snapshots ADD CONSTRAINT non_negative_cost_by_repo_total_cost_usd CHECK (total_cost_usd >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'cost_by_repo_snapshots'::regclass AND conname = 'non_negative_cost_by_repo_total_tokens'
    ) THEN
        ALTER TABLE cost_by_repo_snapshots ADD CONSTRAINT non_negative_cost_by_repo_total_tokens CHECK (total_tokens >= 0);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: cost_by_repo_snapshots ----


CREATE INDEX IF NOT EXISTS idx_cost_by_repo_snapshots_total_cost_usd
    ON cost_by_repo_snapshots (total_cost_usd DESC);

CREATE INDEX IF NOT EXISTS idx_cost_by_repo_snapshots_window
    ON cost_by_repo_snapshots ("window");

CREATE INDEX IF NOT EXISTS idx_cost_by_repo_snapshots_updated_at
    ON cost_by_repo_snapshots (updated_at DESC);

-- ============================================================================
-- TRIGGER: auto-update updated_at
-- ============================================================================
CREATE OR REPLACE FUNCTION update_cost_by_repo_snapshots_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_cost_by_repo_snapshots_updated_at ON cost_by_repo_snapshots;
CREATE TRIGGER trigger_cost_by_repo_snapshots_updated_at
    BEFORE UPDATE ON cost_by_repo_snapshots
    FOR EACH ROW
    EXECUTE FUNCTION update_cost_by_repo_snapshots_updated_at();
