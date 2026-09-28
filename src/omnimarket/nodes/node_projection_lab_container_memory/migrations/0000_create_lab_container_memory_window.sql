-- =============================================================================
-- MIGRATION: lab container memory read model
-- =============================================================================
-- Ticket:  OMN-19961 (task 5b of the .202 memory hardening plan, epic OMN-19955)
-- Owner:   omnimarket.nodes.node_projection_lab_container_memory
-- Version: 1.0.0
--
-- WHY THIS EXISTS
--   Each lab host's census pass publishes one lane container memory event
--   (OMN-19959): every lane container's cgroup memory.max, memory.peak since
--   the container started, limit hits and OOM kills, plus the GitHub Actions
--   jobs that ran on that host inside the window. The event log is the truth;
--   this table is its rebuildable read model, so the windows that carried CI
--   jobs can be queried beside the peaks they produced.
--
-- WHY ONE ROW PER RECORD PER WINDOW
--   A latest-row table (the runner_fleet_liveness shape) keeps only the last
--   window per container and discards the ones that carried the CI bursts.
--
-- WHY record_key IS THE WHOLE KEY
--   The producer assigns record_key = sha256(host_boot_id | container_id |
--   window_end). A redelivery, or a full replay from offset zero, writes the
--   same key and the same values, so ON CONFLICT (record_key) DO UPDATE is
--   idempotent. A re-run census pass is a new window_end and a new row.
--
-- WHY THERE IS NO projection_cursor
--   Nothing serves this table through the projection API yet. With no
--   BIGSERIAL there is no sequence to grant.
--
-- WHY omninode_internal, EXPLICITLY QUALIFIED
--   omnibase_infra's scripts/ci/check_application_database_sql.py (OMN-15361)
--   rejects an unqualified application relation and prohibits `public`. No
--   CREATE SCHEMA here: the node-owned migration loop connects to the
--   application database where omninode_internal already exists (OMN-16759).
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.lab_container_memory_window (
    record_key            TEXT        NOT NULL,

    -- The census host name (omnipc2 on .202) and its boot, which scopes every
    -- counter: a reboot resets the cgroups and restarts the window chain.
    host                  TEXT        NOT NULL,
    host_boot_id          TEXT        NOT NULL,

    lane                  TEXT        NOT NULL,
    container_id          TEXT        NOT NULL,
    container_name        TEXT        NOT NULL,
    container_started_at  TIMESTAMPTZ NOT NULL,

    -- cgroup memory.max in bytes. NULL when it reads `max`, meaning no limit
    -- is set (the Redpanda containers today), which is a fact and not a gap.
    limit_bytes           BIGINT,

    -- cgroup memory.peak: the high-water mark since the container started,
    -- NOT a per-pass peak. It covers [peak_window_start, peak_window_end],
    -- which is [container_started_at, window_end].
    peak_bytes            BIGINT      NOT NULL,
    peak_window_start     TIMESTAMPTZ NOT NULL,
    peak_window_end       TIMESTAMPTZ NOT NULL,

    -- The census window this observation closes.
    window_start          TIMESTAMPTZ NOT NULL,
    window_end            TIMESTAMPTZ NOT NULL,

    -- memory.events `max` (limit hits) and `oom_kill`, as totals and as the
    -- rise inside this window.
    max_total             BIGINT      NOT NULL,
    max_delta             BIGINT      NOT NULL,
    oom_kill_total        BIGINT      NOT NULL,
    oom_kill_delta        BIGINT      NOT NULL,

    -- Every CI job on this host inside [window_start, window_end], each
    -- {repo, run_id, runner_name, job_started_at, job_completed_at}.
    ci_runs               JSONB       NOT NULL DEFAULT '[]'::jsonb,

    -- The writer's clock: when this row was last written. Not an event time.
    projected_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_lab_container_memory_window PRIMARY KEY (record_key),
    CONSTRAINT ck_lab_container_memory_window_counters
        CHECK (
            peak_bytes >= 0 AND max_total >= 0 AND max_delta >= 0
            AND oom_kill_total >= 0 AND oom_kill_delta >= 0
        )
);

-- COLUMN RECONCILIATION (OMN-15376 class). CREATE TABLE IF NOT EXISTS no-ops
-- against a pre-existing table of the same name whatever its shape, so each
-- declared column is re-asserted. NOT NULL is left to the CREATE: Postgres
-- refuses a NOT NULL column added without a default to a table with rows.
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS record_key TEXT;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS host TEXT;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS host_boot_id TEXT;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS lane TEXT;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS container_id TEXT;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS container_name TEXT;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS container_started_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS limit_bytes BIGINT;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS peak_bytes BIGINT;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS peak_window_start TIMESTAMPTZ;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS peak_window_end TIMESTAMPTZ;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS window_start TIMESTAMPTZ;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS window_end TIMESTAMPTZ;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS max_total BIGINT;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS max_delta BIGINT;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS oom_kill_total BIGINT;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS oom_kill_delta BIGINT;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS ci_runs JSONB DEFAULT '[]'::jsonb;
ALTER TABLE omninode_internal.lab_container_memory_window
    ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ DEFAULT NOW();

-- The two reads the plan names: a host's windows in time order, and one
-- container's windows (Prerequisite C joins these against CI runs).
CREATE INDEX IF NOT EXISTS idx_lab_container_memory_window_host_window_end
    ON omninode_internal.lab_container_memory_window (host, window_end DESC);

CREATE INDEX IF NOT EXISTS idx_lab_container_memory_window_container
    ON omninode_internal.lab_container_memory_window (container_name, window_end DESC);

COMMENT ON TABLE omninode_internal.lab_container_memory_window IS
    'OMN-19961: one row per lane container per census window, keyed on the '
    'producer-assigned record_key. Written only by '
    'node_projection_lab_container_memory from '
    'onex.evt.omnibase-infra.lane-container-memory.v1.';
