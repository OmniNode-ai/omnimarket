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
