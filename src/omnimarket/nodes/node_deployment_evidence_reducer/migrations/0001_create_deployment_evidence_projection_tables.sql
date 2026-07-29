-- OMN-12578 Phase 3: reducer-owned deployment evidence projection sink.
--
-- node_deployment_evidence_reducer materializes append-only evidence
-- validation events into these projection tables. The readiness gate consumes
-- this reducer-owned state (not logs or workflow summaries). Corrections and
-- supersessions are new events; prior rows are upserted by deployment_id, the
-- reducer's projection ordering authority is ingest_sequence.

CREATE TABLE IF NOT EXISTS deployment_evidence_projection (
    deployment_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    validation_run_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    evidence_lifecycle_state TEXT NOT NULL,
    validation_state TEXT NOT NULL,
    readiness_state TEXT NOT NULL,
    topology_affecting BOOLEAN NOT NULL DEFAULT FALSE,
    blocking_reason_codes TEXT NOT NULL DEFAULT '',
    contract_hash TEXT NOT NULL,
    evidence_bundle_hash TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

-- ---- BEGIN OMN-15376 shape reconciliation: deployment_evidence_projection ----
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

ALTER TABLE deployment_evidence_projection ADD COLUMN IF NOT EXISTS deployment_id TEXT;
ALTER TABLE deployment_evidence_projection ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE deployment_evidence_projection ADD COLUMN IF NOT EXISTS ticket_id TEXT;
ALTER TABLE deployment_evidence_projection ADD COLUMN IF NOT EXISTS validation_run_id TEXT;
ALTER TABLE deployment_evidence_projection ADD COLUMN IF NOT EXISTS repository TEXT;
ALTER TABLE deployment_evidence_projection ADD COLUMN IF NOT EXISTS evidence_lifecycle_state TEXT;
ALTER TABLE deployment_evidence_projection ADD COLUMN IF NOT EXISTS validation_state TEXT;
ALTER TABLE deployment_evidence_projection ADD COLUMN IF NOT EXISTS readiness_state TEXT;
ALTER TABLE deployment_evidence_projection ADD COLUMN IF NOT EXISTS topology_affecting BOOLEAN DEFAULT FALSE;
ALTER TABLE deployment_evidence_projection ADD COLUMN IF NOT EXISTS blocking_reason_codes TEXT DEFAULT '';
ALTER TABLE deployment_evidence_projection ADD COLUMN IF NOT EXISTS contract_hash TEXT;
ALTER TABLE deployment_evidence_projection ADD COLUMN IF NOT EXISTS evidence_bundle_hash TEXT;
ALTER TABLE deployment_evidence_projection ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['deployment_id', 'correlation_id', 'ticket_id', 'validation_run_id', 'repository', 'evidence_lifecycle_state', 'validation_state', 'readiness_state', 'topology_affecting', 'blocking_reason_codes', 'contract_hash', 'evidence_bundle_hash', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'deployment_evidence_projection'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'deployment_evidence_projection'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge deployment_evidence_projection.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'deployment_evidence_projection'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE deployment_evidence_projection ADD CONSTRAINT deployment_evidence_projection_pkey PRIMARY KEY (deployment_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: deployment_evidence_projection ----


CREATE TABLE IF NOT EXISTS deployment_readiness_projection (
    deployment_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    validation_run_id TEXT NOT NULL,
    readiness_state TEXT NOT NULL,
    blocking_reason_codes TEXT NOT NULL DEFAULT '',
    gap_report_hash TEXT NOT NULL,
    validator_version TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

-- ---- BEGIN OMN-15376 shape reconciliation: deployment_readiness_projection ----
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

ALTER TABLE deployment_readiness_projection ADD COLUMN IF NOT EXISTS deployment_id TEXT;
ALTER TABLE deployment_readiness_projection ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE deployment_readiness_projection ADD COLUMN IF NOT EXISTS validation_run_id TEXT;
ALTER TABLE deployment_readiness_projection ADD COLUMN IF NOT EXISTS readiness_state TEXT;
ALTER TABLE deployment_readiness_projection ADD COLUMN IF NOT EXISTS blocking_reason_codes TEXT DEFAULT '';
ALTER TABLE deployment_readiness_projection ADD COLUMN IF NOT EXISTS gap_report_hash TEXT;
ALTER TABLE deployment_readiness_projection ADD COLUMN IF NOT EXISTS validator_version TEXT;
ALTER TABLE deployment_readiness_projection ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['deployment_id', 'correlation_id', 'validation_run_id', 'readiness_state', 'blocking_reason_codes', 'gap_report_hash', 'validator_version', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'deployment_readiness_projection'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'deployment_readiness_projection'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge deployment_readiness_projection.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'deployment_readiness_projection'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE deployment_readiness_projection ADD CONSTRAINT deployment_readiness_projection_pkey PRIMARY KEY (deployment_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: deployment_readiness_projection ----


CREATE INDEX IF NOT EXISTS idx_deployment_evidence_projection_ticket
    ON deployment_evidence_projection (ticket_id);
CREATE INDEX IF NOT EXISTS idx_deployment_evidence_projection_updated_at
    ON deployment_evidence_projection (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_deployment_evidence_projection_readiness
    ON deployment_evidence_projection (readiness_state);

CREATE INDEX IF NOT EXISTS idx_deployment_readiness_projection_updated_at
    ON deployment_readiness_projection (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_deployment_readiness_projection_readiness
    ON deployment_readiness_projection (readiness_state);
