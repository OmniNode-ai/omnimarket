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
