-- OMN-12842 (M2): durable, scored capsule/exemplar store projection.
--
-- A capsule is identified by a deterministic capsule_hash; a changed exemplar
-- (different content / commit / artifact / schema_version) is a NEW row, never
-- an in-place mutation. Effectiveness is populated from context-ROI score
-- events and is NEVER empty on a scored row -- enforced by a CHECK constraint,
-- not a comment. Raw scored values are immutable in this base table; staleness
-- decay is applied at read time by the projection read view.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS capsule_store (
    capsule_id UUID PRIMARY KEY,
    capsule_hash TEXT NOT NULL,
    factor TEXT NOT NULL,
    source_commit TEXT NOT NULL,
    source_artifact TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    validity_scope TEXT NOT NULL,
    success_rate NUMERIC(6, 5) NOT NULL
        CHECK (success_rate >= 0 AND success_rate <= 1),
    first_pass_rate NUMERIC(6, 5) NOT NULL
        CHECK (first_pass_rate >= 0 AND first_pass_rate <= 1),
    cost_per_success NUMERIC(18, 6) NOT NULL CHECK (cost_per_success >= 0),
    hit_count BIGINT NOT NULL CHECK (hit_count >= 1),
    last_scored TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- "No empty effectiveness fields" encoded in the schema: a scored row
    -- (hit_count >= 1) must carry all effectiveness values. The NOT NULL
    -- columns already guarantee this; the explicit constraint documents and
    -- enforces the scored-row invariant for future nullable migrations.
    CONSTRAINT capsule_store_scored_effectiveness_non_null
        CHECK (
            hit_count >= 1
            AND success_rate IS NOT NULL
            AND first_pass_rate IS NOT NULL
            AND cost_per_success IS NOT NULL
            AND last_scored IS NOT NULL
        )
);

-- ---- BEGIN OMN-15376 shape reconciliation: capsule_store ----
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

ALTER TABLE capsule_store ADD COLUMN IF NOT EXISTS capsule_id UUID;
ALTER TABLE capsule_store ADD COLUMN IF NOT EXISTS capsule_hash TEXT;
ALTER TABLE capsule_store ADD COLUMN IF NOT EXISTS factor TEXT;
ALTER TABLE capsule_store ADD COLUMN IF NOT EXISTS source_commit TEXT;
ALTER TABLE capsule_store ADD COLUMN IF NOT EXISTS source_artifact TEXT;
ALTER TABLE capsule_store ADD COLUMN IF NOT EXISTS schema_version TEXT;
ALTER TABLE capsule_store ADD COLUMN IF NOT EXISTS validity_scope TEXT;
ALTER TABLE capsule_store ADD COLUMN IF NOT EXISTS success_rate NUMERIC(6, 5);
ALTER TABLE capsule_store ADD COLUMN IF NOT EXISTS first_pass_rate NUMERIC(6, 5);
ALTER TABLE capsule_store ADD COLUMN IF NOT EXISTS cost_per_success NUMERIC(18, 6);
ALTER TABLE capsule_store ADD COLUMN IF NOT EXISTS hit_count BIGINT;
ALTER TABLE capsule_store ADD COLUMN IF NOT EXISTS last_scored TIMESTAMPTZ;
ALTER TABLE capsule_store ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE capsule_store ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['capsule_id', 'capsule_hash', 'factor', 'source_commit', 'source_artifact', 'schema_version', 'validity_scope', 'success_rate', 'first_pass_rate', 'cost_per_success', 'hit_count', 'last_scored', 'created_at', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'capsule_store'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'capsule_store'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge capsule_store.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'capsule_store'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE capsule_store ADD CONSTRAINT capsule_store_pkey PRIMARY KEY (capsule_id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'capsule_store'::regclass AND conname = 'capsule_store_success_rate_check'
    ) THEN
        ALTER TABLE capsule_store ADD CONSTRAINT capsule_store_success_rate_check CHECK (success_rate >= 0 AND success_rate <= 1);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'capsule_store'::regclass AND conname = 'capsule_store_first_pass_rate_check'
    ) THEN
        ALTER TABLE capsule_store ADD CONSTRAINT capsule_store_first_pass_rate_check CHECK (first_pass_rate >= 0 AND first_pass_rate <= 1);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'capsule_store'::regclass AND conname = 'capsule_store_cost_per_success_check'
    ) THEN
        ALTER TABLE capsule_store ADD CONSTRAINT capsule_store_cost_per_success_check CHECK (cost_per_success >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'capsule_store'::regclass AND conname = 'capsule_store_hit_count_check'
    ) THEN
        ALTER TABLE capsule_store ADD CONSTRAINT capsule_store_hit_count_check CHECK (hit_count >= 1);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'capsule_store'::regclass AND conname = 'capsule_store_scored_effectiveness_non_null'
    ) THEN
        ALTER TABLE capsule_store ADD CONSTRAINT capsule_store_scored_effectiveness_non_null CHECK ( hit_count >= 1 AND success_rate IS NOT NULL AND first_pass_rate IS NOT NULL AND cost_per_success IS NOT NULL AND last_scored IS NOT NULL );
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: capsule_store ----


CREATE UNIQUE INDEX IF NOT EXISTS ux_capsule_identity
    ON capsule_store (capsule_hash);

CREATE INDEX IF NOT EXISTS ix_capsule_store_factor_scope
    ON capsule_store (factor, validity_scope);

CREATE OR REPLACE FUNCTION refresh_capsule_store_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_capsule_store_updated_at ON capsule_store;
CREATE TRIGGER trg_capsule_store_updated_at
    BEFORE UPDATE ON capsule_store
    FOR EACH ROW
    EXECUTE FUNCTION refresh_capsule_store_updated_at();
-- The decay read view (projection_capsule_effectiveness) is created in the
-- next migration (079) so the base table exists before any view that reads it
-- (enforced by scripts/ci/check_migration_base_before_view.py, OMN-12942).
