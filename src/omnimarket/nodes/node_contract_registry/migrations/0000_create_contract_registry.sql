-- OMN-11243: Create the contract_registry projection table.
-- Stores validated contract snapshots for each omnimarket node.
-- DDL owner: omnimarket.nodes.node_contract_registry

CREATE TABLE IF NOT EXISTS contract_registry (
    id SERIAL PRIMARY KEY,
    node_name TEXT NOT NULL,
    contract_hash TEXT NOT NULL,
    contract_yaml TEXT NOT NULL,
    node_version JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    correlation_id UUID NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deployer_id TEXT NOT NULL DEFAULT '',
    target_profile TEXT NOT NULL DEFAULT '',
    UNIQUE(node_name, contract_hash)
);

-- ---- BEGIN OMN-15376 shape reconciliation: contract_registry ----
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

ALTER TABLE contract_registry ADD COLUMN IF NOT EXISTS id SERIAL;
ALTER TABLE contract_registry ADD COLUMN IF NOT EXISTS node_name TEXT;
ALTER TABLE contract_registry ADD COLUMN IF NOT EXISTS contract_hash TEXT;
ALTER TABLE contract_registry ADD COLUMN IF NOT EXISTS contract_yaml TEXT;
ALTER TABLE contract_registry ADD COLUMN IF NOT EXISTS node_version JSONB DEFAULT '{}';
ALTER TABLE contract_registry ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE contract_registry ADD COLUMN IF NOT EXISTS correlation_id UUID;
ALTER TABLE contract_registry ADD COLUMN IF NOT EXISTS registered_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE contract_registry ADD COLUMN IF NOT EXISTS deployer_id TEXT DEFAULT '';
ALTER TABLE contract_registry ADD COLUMN IF NOT EXISTS target_profile TEXT DEFAULT '';

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'node_name', 'contract_hash', 'contract_yaml', 'node_version', 'status', 'correlation_id', 'registered_at', 'deployer_id', 'target_profile']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'contract_registry'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'contract_registry'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge contract_registry.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'contract_registry'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE contract_registry ADD CONSTRAINT contract_registry_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'contract_registry'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['contract_hash', 'node_name']::text[]
    ) THEN
        ALTER TABLE contract_registry ADD CONSTRAINT contract_registry_node_name_contract_hash_key UNIQUE(node_name, contract_hash);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: contract_registry ----


CREATE INDEX IF NOT EXISTS idx_contract_registry_node_name ON contract_registry(node_name);
CREATE INDEX IF NOT EXISTS idx_contract_registry_status ON contract_registry(status);
