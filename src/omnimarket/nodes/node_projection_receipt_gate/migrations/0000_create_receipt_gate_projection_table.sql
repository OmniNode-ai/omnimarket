-- Migration: 0000_create_receipt_gate_projection_table.sql
-- Node: node_projection_receipt_gate
-- Ticket: OMN-13081
-- Creates receipt_gate_rows table for the receipt-gate projection API.
-- This table backs the projection API endpoint for
-- onex.snapshot.projection.receipt-gate.v1, consumed by the omnidash
-- receipt-gate widget.

CREATE TABLE IF NOT EXISTS receipt_gate_rows (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    pass BOOLEAN NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    pr_ref TEXT,
    worker TEXT,
    verifier TEXT,
    evidence_count INTEGER,
    evidence_hash TEXT,
    signed_at TEXT,
    observed_at TIMESTAMPTZ NOT NULL
);

-- ---- BEGIN OMN-15376 shape reconciliation: receipt_gate_rows ----
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

ALTER TABLE receipt_gate_rows ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE receipt_gate_rows ADD COLUMN IF NOT EXISTS name TEXT;
ALTER TABLE receipt_gate_rows ADD COLUMN IF NOT EXISTS pass BOOLEAN;
ALTER TABLE receipt_gate_rows ADD COLUMN IF NOT EXISTS detail TEXT DEFAULT '';
ALTER TABLE receipt_gate_rows ADD COLUMN IF NOT EXISTS pr_ref TEXT;
ALTER TABLE receipt_gate_rows ADD COLUMN IF NOT EXISTS worker TEXT;
ALTER TABLE receipt_gate_rows ADD COLUMN IF NOT EXISTS verifier TEXT;
ALTER TABLE receipt_gate_rows ADD COLUMN IF NOT EXISTS evidence_count INTEGER;
ALTER TABLE receipt_gate_rows ADD COLUMN IF NOT EXISTS evidence_hash TEXT;
ALTER TABLE receipt_gate_rows ADD COLUMN IF NOT EXISTS signed_at TEXT;
ALTER TABLE receipt_gate_rows ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'name', 'pass', 'detail', 'observed_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'receipt_gate_rows'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'receipt_gate_rows'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge receipt_gate_rows.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'receipt_gate_rows'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE receipt_gate_rows ADD CONSTRAINT receipt_gate_rows_pkey PRIMARY KEY (id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: receipt_gate_rows ----


CREATE INDEX IF NOT EXISTS receipt_gate_rows_observed_at_idx
    ON receipt_gate_rows (observed_at DESC);

CREATE INDEX IF NOT EXISTS receipt_gate_rows_name_idx
    ON receipt_gate_rows (name);

CREATE INDEX IF NOT EXISTS receipt_gate_rows_pass_idx
    ON receipt_gate_rows (pass);
