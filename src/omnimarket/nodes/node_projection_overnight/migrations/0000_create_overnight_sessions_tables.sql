-- OMN-12936: base tables for the overnight readiness projection.
--
-- The 0001 view (projection_overnight_readiness) reads from overnight_sessions.
-- Those base tables were previously created only through the omnibase_infra
-- schema path (schema_overnight_sessions.sql), which the omnimarket projection
-- migration runner does not apply. As a result the projection-api startup found
-- no projection_overnight_readiness relation and served HTTP 503
-- "table 'public.projection_overnight_readiness' not found at startup".
--
-- Shipping the base-table DDL as a node-owned migration ordered before the view
-- makes the omnimarket migration runner self-sufficient: it creates the base
-- tables, then the 0001 view materializes against them. The DDL is identical to
-- omnibase_infra schema_overnight_sessions.sql and fully idempotent
-- (IF NOT EXISTS throughout), so re-applying over an infra-seeded database is a
-- no-op.
--
-- Related:
--   - OMN-8455: W2.8 overnight_sessions migration + node_projection_overnight
--   - node_projection_overnight/migrations/0001_create_overnight_readiness_projection_view.sql

CREATE TABLE IF NOT EXISTS overnight_sessions (
    session_id           TEXT PRIMARY KEY,             -- correlation_id from executor
    session_start_ts     TIMESTAMPTZ NOT NULL,
    contract_path        TEXT,
    dry_run              BOOLEAN NOT NULL DEFAULT FALSE,

    -- Aggregate metrics (updated on session-completed)
    phases_run           TEXT[] NOT NULL DEFAULT '{}',
    phases_failed        TEXT[] NOT NULL DEFAULT '{}',
    phases_skipped       TEXT[] NOT NULL DEFAULT '{}',
    dispatch_count       INT NOT NULL DEFAULT 0,

    -- Halt tracking
    halt_reason          TEXT,

    -- Terminal state
    session_status       TEXT NOT NULL DEFAULT 'in_progress',
    session_end_ts       TIMESTAMPTZ,

    -- Lifecycle
    accumulated_cost_usd NUMERIC(10,4) NOT NULL DEFAULT 0,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT valid_session_status CHECK (
        session_status IN ('in_progress', 'completed', 'partial', 'failed')
    )
);

-- ---- BEGIN OMN-15376 shape reconciliation: overnight_sessions ----
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

ALTER TABLE overnight_sessions ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE overnight_sessions ADD COLUMN IF NOT EXISTS session_start_ts TIMESTAMPTZ;
ALTER TABLE overnight_sessions ADD COLUMN IF NOT EXISTS contract_path TEXT;
ALTER TABLE overnight_sessions ADD COLUMN IF NOT EXISTS dry_run BOOLEAN DEFAULT FALSE;
ALTER TABLE overnight_sessions ADD COLUMN IF NOT EXISTS phases_run TEXT[] DEFAULT '{}';
ALTER TABLE overnight_sessions ADD COLUMN IF NOT EXISTS phases_failed TEXT[] DEFAULT '{}';
ALTER TABLE overnight_sessions ADD COLUMN IF NOT EXISTS phases_skipped TEXT[] DEFAULT '{}';
ALTER TABLE overnight_sessions ADD COLUMN IF NOT EXISTS dispatch_count INT DEFAULT 0;
ALTER TABLE overnight_sessions ADD COLUMN IF NOT EXISTS halt_reason TEXT;
ALTER TABLE overnight_sessions ADD COLUMN IF NOT EXISTS session_status TEXT DEFAULT 'in_progress';
ALTER TABLE overnight_sessions ADD COLUMN IF NOT EXISTS session_end_ts TIMESTAMPTZ;
ALTER TABLE overnight_sessions ADD COLUMN IF NOT EXISTS accumulated_cost_usd NUMERIC(10,4) DEFAULT 0;
ALTER TABLE overnight_sessions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['session_id', 'session_start_ts', 'dry_run', 'phases_run', 'phases_failed', 'phases_skipped', 'dispatch_count', 'session_status', 'accumulated_cost_usd', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'overnight_sessions'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'overnight_sessions'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge overnight_sessions.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'overnight_sessions'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE overnight_sessions ADD CONSTRAINT overnight_sessions_pkey PRIMARY KEY (session_id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'overnight_sessions'::regclass AND conname = 'valid_session_status'
    ) THEN
        ALTER TABLE overnight_sessions ADD CONSTRAINT valid_session_status CHECK ( session_status IN ('in_progress', 'completed', 'partial', 'failed') );
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: overnight_sessions ----


-- Normalized phase results table — avoids JSONB overhead, enables per-phase indexing
CREATE TABLE IF NOT EXISTS overnight_session_phases (
    id                   BIGSERIAL PRIMARY KEY,
    session_id           TEXT NOT NULL REFERENCES overnight_sessions(session_id) ON DELETE CASCADE,
    phase_name           TEXT NOT NULL,
    phase_status         TEXT NOT NULL,               -- success | failed | skipped
    duration_ms          INT NOT NULL DEFAULT 0,
    side_effect_summary  TEXT NOT NULL DEFAULT '',
    error_message        TEXT,
    sequence_number      INT NOT NULL DEFAULT 0,
    recorded_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT valid_phase_status CHECK (
        phase_status IN ('success', 'failed', 'skipped')
    )
);

-- ---- BEGIN OMN-15376 shape reconciliation: overnight_session_phases ----
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

ALTER TABLE overnight_session_phases ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE overnight_session_phases ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE overnight_session_phases ADD COLUMN IF NOT EXISTS phase_name TEXT;
ALTER TABLE overnight_session_phases ADD COLUMN IF NOT EXISTS phase_status TEXT;
ALTER TABLE overnight_session_phases ADD COLUMN IF NOT EXISTS duration_ms INT DEFAULT 0;
ALTER TABLE overnight_session_phases ADD COLUMN IF NOT EXISTS side_effect_summary TEXT DEFAULT '';
ALTER TABLE overnight_session_phases ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE overnight_session_phases ADD COLUMN IF NOT EXISTS sequence_number INT DEFAULT 0;
ALTER TABLE overnight_session_phases ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'session_id', 'phase_name', 'phase_status', 'duration_ms', 'side_effect_summary', 'sequence_number', 'recorded_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'overnight_session_phases'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'overnight_session_phases'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge overnight_session_phases.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'overnight_session_phases'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE overnight_session_phases ADD CONSTRAINT overnight_session_phases_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'overnight_session_phases'::regclass AND conname = 'overnight_session_phases_session_id_fkey'
    ) THEN
        ALTER TABLE overnight_session_phases ADD CONSTRAINT overnight_session_phases_session_id_fkey FOREIGN KEY (session_id) REFERENCES overnight_sessions(session_id) ON DELETE CASCADE;
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'overnight_session_phases'::regclass AND conname = 'valid_phase_status'
    ) THEN
        ALTER TABLE overnight_session_phases ADD CONSTRAINT valid_phase_status CHECK ( phase_status IN ('success', 'failed', 'skipped') );
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: overnight_session_phases ----


CREATE UNIQUE INDEX IF NOT EXISTS ix_session_phases_unique
    ON overnight_session_phases(session_id, phase_name, sequence_number);

CREATE INDEX IF NOT EXISTS ix_overnight_sessions_status
    ON overnight_sessions(session_status);

CREATE INDEX IF NOT EXISTS ix_overnight_sessions_start
    ON overnight_sessions(session_start_ts DESC);

CREATE INDEX IF NOT EXISTS ix_overnight_session_phases_session
    ON overnight_session_phases(session_id);
