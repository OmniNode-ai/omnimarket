-- Migration: 0000_create_gate_projection_tables.sql
-- Node: node_omnigate_projection
-- Ticket: OMN-13067
-- Creates gate_activity and gate_metrics tables for the OmniGate projection API.
-- These tables back the projection API endpoints for
-- onex.snapshot.projection.gate.activity.v1 and
-- onex.snapshot.projection.gate.metrics.v1.

CREATE TABLE IF NOT EXISTS gate_activity (
    id BIGSERIAL PRIMARY KEY,
    repository_id TEXT NOT NULL,
    project_name TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    base_sha TEXT NOT NULL DEFAULT '',
    head_sha TEXT NOT NULL DEFAULT '',
    diff_hash TEXT,
    config_hash TEXT,
    status TEXT NOT NULL,
    action TEXT,
    reason TEXT NOT NULL DEFAULT '',
    total_checks INTEGER NOT NULL DEFAULT 0,
    failed_checks INTEGER NOT NULL DEFAULT 0,
    advisory_checks INTEGER NOT NULL DEFAULT 0,
    pending_checks INTEGER NOT NULL DEFAULT 0,
    observed_at TIMESTAMPTZ NOT NULL
);

-- ---- BEGIN OMN-15376 shape reconciliation: gate_activity ----
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

ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS repository_id TEXT;
ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS project_name TEXT DEFAULT '';
ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS branch TEXT DEFAULT '';
ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS base_sha TEXT DEFAULT '';
ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS head_sha TEXT DEFAULT '';
ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS diff_hash TEXT;
ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS config_hash TEXT;
ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS action TEXT;
ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS reason TEXT DEFAULT '';
ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS total_checks INTEGER DEFAULT 0;
ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS failed_checks INTEGER DEFAULT 0;
ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS advisory_checks INTEGER DEFAULT 0;
ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS pending_checks INTEGER DEFAULT 0;
ALTER TABLE gate_activity ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'repository_id', 'project_name', 'branch', 'base_sha', 'head_sha', 'status', 'reason', 'total_checks', 'failed_checks', 'advisory_checks', 'pending_checks', 'observed_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'gate_activity'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'gate_activity'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge gate_activity.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'gate_activity'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE gate_activity ADD CONSTRAINT gate_activity_pkey PRIMARY KEY (id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: gate_activity ----


CREATE INDEX IF NOT EXISTS gate_activity_observed_at_idx
    ON gate_activity (observed_at DESC);

CREATE INDEX IF NOT EXISTS gate_activity_repository_id_idx
    ON gate_activity (repository_id);

CREATE INDEX IF NOT EXISTS gate_activity_status_idx
    ON gate_activity (status);

-- Aggregate metrics snapshot — single row upserted on each event.
-- id=1 is the canonical singleton row.
CREATE TABLE IF NOT EXISTS gate_metrics (
    id INTEGER PRIMARY KEY DEFAULT 1,
    total_events INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    advisory INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: gate_metrics ----
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

ALTER TABLE gate_metrics ADD COLUMN IF NOT EXISTS id INTEGER DEFAULT 1;
ALTER TABLE gate_metrics ADD COLUMN IF NOT EXISTS total_events INTEGER DEFAULT 0;
ALTER TABLE gate_metrics ADD COLUMN IF NOT EXISTS passed INTEGER DEFAULT 0;
ALTER TABLE gate_metrics ADD COLUMN IF NOT EXISTS failed INTEGER DEFAULT 0;
ALTER TABLE gate_metrics ADD COLUMN IF NOT EXISTS advisory INTEGER DEFAULT 0;
ALTER TABLE gate_metrics ADD COLUMN IF NOT EXISTS pending INTEGER DEFAULT 0;
ALTER TABLE gate_metrics ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'total_events', 'passed', 'failed', 'advisory', 'pending', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'gate_metrics'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'gate_metrics'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge gate_metrics.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'gate_metrics'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE gate_metrics ADD CONSTRAINT gate_metrics_pkey PRIMARY KEY (id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: gate_metrics ----


-- Seed the singleton metrics row so the projection API returns 1 row immediately.
INSERT INTO gate_metrics (id, total_events, passed, failed, advisory, pending, updated_at)
VALUES (1, 0, 0, 0, 0, 0, NOW())
ON CONFLICT (id) DO NOTHING;
