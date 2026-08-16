-- OMN-13078: intent_classification_events projection table.
-- Target DB: omnidash_analytics (omnibase_infra postgres on .201:5436)
-- Node: node_projection_intent_classification
-- UPSERT key: correlation_id (latest-wins)
-- Feeds omnidash widgets: intent-distribution, session-timeline

CREATE TABLE IF NOT EXISTS intent_classification_events (
    id             BIGSERIAL PRIMARY KEY,
    correlation_id TEXT UNIQUE NOT NULL,
    session_id     TEXT NOT NULL,
    intent_class   TEXT NOT NULL,
    confidence     FLOAT NOT NULL DEFAULT 0.0,
    keywords       TEXT[] NOT NULL DEFAULT '{}',
    emitted_at     TIMESTAMPTZ NOT NULL,
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- OMN-14751: dispatcher provenance ('claude' | 'cursor'). Nullable by
    -- design: rows projected from events that predate the field stay NULL.
    agent_source   TEXT
);

-- ---- BEGIN OMN-15376 shape reconciliation: intent_classification_events ----
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

ALTER TABLE intent_classification_events ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE intent_classification_events ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE intent_classification_events ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE intent_classification_events ADD COLUMN IF NOT EXISTS intent_class TEXT;
ALTER TABLE intent_classification_events ADD COLUMN IF NOT EXISTS confidence FLOAT DEFAULT 0.0;
ALTER TABLE intent_classification_events ADD COLUMN IF NOT EXISTS keywords TEXT[] DEFAULT '{}';
ALTER TABLE intent_classification_events ADD COLUMN IF NOT EXISTS emitted_at TIMESTAMPTZ;
ALTER TABLE intent_classification_events ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE intent_classification_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE intent_classification_events ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
-- agent_source stays nullable (see CREATE TABLE) -- deliberately NOT in the
-- NOT NULL convergence loop below.
ALTER TABLE intent_classification_events ADD COLUMN IF NOT EXISTS agent_source TEXT;

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'correlation_id', 'session_id', 'intent_class', 'confidence', 'keywords', 'emitted_at', 'ingested_at', 'created_at', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'intent_classification_events'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'intent_classification_events'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge intent_classification_events.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'intent_classification_events'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE intent_classification_events ADD CONSTRAINT intent_classification_events_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'intent_classification_events'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['correlation_id']::text[]
    ) THEN
        ALTER TABLE intent_classification_events ADD CONSTRAINT intent_classification_events_correlation_id_key UNIQUE (correlation_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: intent_classification_events ----


CREATE INDEX IF NOT EXISTS idx_intent_classification_events_session_id
    ON intent_classification_events (session_id);

CREATE INDEX IF NOT EXISTS idx_intent_classification_events_intent_class
    ON intent_classification_events (intent_class);

CREATE INDEX IF NOT EXISTS idx_intent_classification_events_emitted_at
    ON intent_classification_events (emitted_at);

CREATE OR REPLACE FUNCTION refresh_intent_classification_events_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_intent_classification_events_updated_at
    ON intent_classification_events;
CREATE TRIGGER trg_intent_classification_events_updated_at
    BEFORE UPDATE ON intent_classification_events
    FOR EACH ROW
    EXECUTE FUNCTION refresh_intent_classification_events_updated_at();
