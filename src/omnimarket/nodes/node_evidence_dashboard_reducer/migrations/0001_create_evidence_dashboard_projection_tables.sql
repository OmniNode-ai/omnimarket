-- OMN-11473: projection tables for evidence dashboard Waves 2/3.

CREATE TABLE IF NOT EXISTS evidence_dashboard_projection (
    projection_key TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    ticket_id TEXT,
    repo TEXT,
    pr_number INT,
    validation_run_id TEXT,
    current_stage TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    projection_cursor TEXT NOT NULL,
    last_event_id TEXT NOT NULL,
    last_ingest_sequence BIGINT,
    freshness_state TEXT NOT NULL,
    degraded_reason TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

-- ---- BEGIN OMN-15376 shape reconciliation: evidence_dashboard_projection ----
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

ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS projection_key TEXT;
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS ticket_id TEXT;
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS repo TEXT;
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS pr_number INT;
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS validation_run_id TEXT;
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS current_stage TEXT;
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS severity TEXT;
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS projection_cursor TEXT;
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS last_event_id TEXT;
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS last_ingest_sequence BIGINT;
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS freshness_state TEXT;
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS degraded_reason TEXT;
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE evidence_dashboard_projection ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['projection_key', 'correlation_id', 'current_stage', 'status', 'severity', 'projection_cursor', 'last_event_id', 'freshness_state', 'observed_at', 'updated_at', 'expires_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'evidence_dashboard_projection'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'evidence_dashboard_projection'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge evidence_dashboard_projection.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'evidence_dashboard_projection'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE evidence_dashboard_projection ADD CONSTRAINT evidence_dashboard_projection_pkey PRIMARY KEY (projection_key);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: evidence_dashboard_projection ----


CREATE TABLE IF NOT EXISTS evidence_correlation_trace_projection (
    event_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    ticket_id TEXT,
    repo TEXT,
    pr_number INT,
    source_topic TEXT NOT NULL,
    source_event_type TEXT NOT NULL,
    normalized_stage TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    ingest_sequence BIGINT,
    projection_cursor TEXT NOT NULL,
    last_event_id TEXT NOT NULL,
    last_ingest_sequence BIGINT,
    freshness_state TEXT NOT NULL,
    degraded_reason TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ---- BEGIN OMN-15376 shape reconciliation: evidence_correlation_trace_projection ----
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

ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS event_id TEXT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS ticket_id TEXT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS repo TEXT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS pr_number INT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS source_topic TEXT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS source_event_type TEXT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS normalized_stage TEXT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS severity TEXT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS ingest_sequence BIGINT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS projection_cursor TEXT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS last_event_id TEXT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS last_ingest_sequence BIGINT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS freshness_state TEXT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS degraded_reason TEXT;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE evidence_correlation_trace_projection ADD COLUMN IF NOT EXISTS payload JSONB DEFAULT '{}'::jsonb;

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['event_id', 'correlation_id', 'source_topic', 'source_event_type', 'normalized_stage', 'status', 'severity', 'projection_cursor', 'last_event_id', 'freshness_state', 'observed_at', 'updated_at', 'expires_at', 'payload']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'evidence_correlation_trace_projection'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'evidence_correlation_trace_projection'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge evidence_correlation_trace_projection.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'evidence_correlation_trace_projection'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE evidence_correlation_trace_projection ADD CONSTRAINT evidence_correlation_trace_projection_pkey PRIMARY KEY (event_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: evidence_correlation_trace_projection ----


CREATE TABLE IF NOT EXISTS evidence_readiness_aggregate_projection (
    aggregate_key TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    ticket_id TEXT,
    repo TEXT,
    pr_number INT,
    readiness_state TEXT NOT NULL,
    total_events INT NOT NULL DEFAULT 0,
    error_events INT NOT NULL DEFAULT 0,
    warning_events INT NOT NULL DEFAULT 0,
    projection_cursor TEXT NOT NULL,
    last_event_id TEXT NOT NULL,
    last_ingest_sequence BIGINT,
    freshness_state TEXT NOT NULL,
    degraded_reason TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

-- ---- BEGIN OMN-15376 shape reconciliation: evidence_readiness_aggregate_projection ----
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

ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS aggregate_key TEXT;
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS ticket_id TEXT;
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS repo TEXT;
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS pr_number INT;
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS readiness_state TEXT;
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS total_events INT DEFAULT 0;
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS error_events INT DEFAULT 0;
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS warning_events INT DEFAULT 0;
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS projection_cursor TEXT;
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS last_event_id TEXT;
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS last_ingest_sequence BIGINT;
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS freshness_state TEXT;
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS degraded_reason TEXT;
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE evidence_readiness_aggregate_projection ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['aggregate_key', 'correlation_id', 'readiness_state', 'total_events', 'error_events', 'warning_events', 'projection_cursor', 'last_event_id', 'freshness_state', 'observed_at', 'updated_at', 'expires_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'evidence_readiness_aggregate_projection'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'evidence_readiness_aggregate_projection'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge evidence_readiness_aggregate_projection.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'evidence_readiness_aggregate_projection'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE evidence_readiness_aggregate_projection ADD CONSTRAINT evidence_readiness_aggregate_projection_pkey PRIMARY KEY (aggregate_key);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: evidence_readiness_aggregate_projection ----


CREATE INDEX IF NOT EXISTS idx_evidence_dashboard_projection_cursor
    ON evidence_dashboard_projection (projection_cursor);
CREATE INDEX IF NOT EXISTS idx_evidence_dashboard_observed_at
    ON evidence_dashboard_projection (observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_dashboard_expires_at
    ON evidence_dashboard_projection (expires_at);
CREATE INDEX IF NOT EXISTS idx_evidence_dashboard_ticket
    ON evidence_dashboard_projection (ticket_id);
CREATE INDEX IF NOT EXISTS idx_evidence_dashboard_repo_pr
    ON evidence_dashboard_projection (repo, pr_number);

CREATE INDEX IF NOT EXISTS idx_evidence_trace_correlation_sequence
    ON evidence_correlation_trace_projection (correlation_id, ingest_sequence, observed_at);
CREATE INDEX IF NOT EXISTS idx_evidence_trace_projection_cursor
    ON evidence_correlation_trace_projection (projection_cursor);
CREATE INDEX IF NOT EXISTS idx_evidence_trace_expires_at
    ON evidence_correlation_trace_projection (expires_at);
CREATE INDEX IF NOT EXISTS idx_evidence_trace_ticket
    ON evidence_correlation_trace_projection (ticket_id);

CREATE INDEX IF NOT EXISTS idx_evidence_readiness_projection_cursor
    ON evidence_readiness_aggregate_projection (projection_cursor);
CREATE INDEX IF NOT EXISTS idx_evidence_readiness_observed_at
    ON evidence_readiness_aggregate_projection (observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_readiness_expires_at
    ON evidence_readiness_aggregate_projection (expires_at);
CREATE INDEX IF NOT EXISTS idx_evidence_readiness_ticket
    ON evidence_readiness_aggregate_projection (ticket_id);
