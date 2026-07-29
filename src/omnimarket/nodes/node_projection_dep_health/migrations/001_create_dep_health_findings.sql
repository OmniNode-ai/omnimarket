-- OMN-11042: Create the dep_health_findings projection table.

CREATE TABLE IF NOT EXISTS dep_health_findings (
  id BIGSERIAL PRIMARY KEY,
  run_id VARCHAR NOT NULL,
  finding_type VARCHAR NOT NULL,
  severity VARCHAR NOT NULL,
  repo VARCHAR NOT NULL,
  file_path VARCHAR NOT NULL DEFAULT '',
  symbol VARCHAR NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  rule_id VARCHAR NOT NULL,
  rule_version VARCHAR NOT NULL,
  captured_at TIMESTAMPTZ NOT NULL,
  UNIQUE (run_id, finding_type, file_path, symbol)
);

-- ---- BEGIN OMN-15376 shape reconciliation: dep_health_findings ----
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

ALTER TABLE dep_health_findings ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE dep_health_findings ADD COLUMN IF NOT EXISTS run_id VARCHAR;
ALTER TABLE dep_health_findings ADD COLUMN IF NOT EXISTS finding_type VARCHAR;
ALTER TABLE dep_health_findings ADD COLUMN IF NOT EXISTS severity VARCHAR;
ALTER TABLE dep_health_findings ADD COLUMN IF NOT EXISTS repo VARCHAR;
ALTER TABLE dep_health_findings ADD COLUMN IF NOT EXISTS file_path VARCHAR DEFAULT '';
ALTER TABLE dep_health_findings ADD COLUMN IF NOT EXISTS symbol VARCHAR DEFAULT '';
ALTER TABLE dep_health_findings ADD COLUMN IF NOT EXISTS detail TEXT DEFAULT '';
ALTER TABLE dep_health_findings ADD COLUMN IF NOT EXISTS rule_id VARCHAR;
ALTER TABLE dep_health_findings ADD COLUMN IF NOT EXISTS rule_version VARCHAR;
ALTER TABLE dep_health_findings ADD COLUMN IF NOT EXISTS captured_at TIMESTAMPTZ;

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'run_id', 'finding_type', 'severity', 'repo', 'file_path', 'symbol', 'detail', 'rule_id', 'rule_version', 'captured_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'dep_health_findings'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'dep_health_findings'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge dep_health_findings.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'dep_health_findings'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE dep_health_findings ADD CONSTRAINT dep_health_findings_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'dep_health_findings'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['file_path', 'finding_type', 'run_id', 'symbol']::text[]
    ) THEN
        ALTER TABLE dep_health_findings ADD CONSTRAINT dep_health_findings_run_id_finding_type_file_path_symbol_key UNIQUE (run_id, finding_type, file_path, symbol);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: dep_health_findings ----


CREATE INDEX IF NOT EXISTS idx_dep_health_findings_run_id
  ON dep_health_findings (run_id);

CREATE INDEX IF NOT EXISTS idx_dep_health_findings_severity
  ON dep_health_findings (severity);

CREATE INDEX IF NOT EXISTS idx_dep_health_findings_repo
  ON dep_health_findings (repo);

CREATE INDEX IF NOT EXISTS idx_dep_health_findings_captured_at
  ON dep_health_findings (captured_at);
