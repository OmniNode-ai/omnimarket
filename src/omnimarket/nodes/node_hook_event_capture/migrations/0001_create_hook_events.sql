-- OMN-16090: node-owned projection migration for hook_events.
--
-- WHY THIS EXISTS
--   Operator machines emit contract-typed hook and skill-lifecycle events
--   through a local emit socket. When that socket is absent the emitter
--   SPOOLS to disk rather than dropping -- correct, but the spool had no
--   drain to anywhere. Measured 2026-08-16 on one operator machine: 1931
--   spooled events, all carrying spool_reason "FileNotFoundError: [Errno 2]
--   No such file or directory", across four families (artifact.captured 751,
--   tool.output.captured 437, skill-started 429, skill-completed 314).
--   Durable on disk, invisible to every projection surface.
--
--   This table is the landing zone for those events once they are submitted
--   through the secure workflow gateway as hook-event-capture batches.
--
-- IDEMPOTENCY KEY IS (tenant_id, event_sha), AND event_id IS NOT USABLE
--   Only two of the four measured families carry an event_id at all;
--   artifact.captured and tool.output.captured have no identity field. Keying
--   on event_id would drop 61% of the corpus or force the writer to invent
--   identity. The submitter computes event_sha = sha256 over the canonical
--   event body, and the UNIQUE constraint below is what makes a redelivered
--   Kafka batch a no-op instead of a duplicate.
--
-- PAYLOAD IS JSONB AND UNINTERPRETED, DELIBERATELY
--   The four families are independently versioned and new families appear
--   without a release of this node. Splitting the body into typed columns here
--   would make this table a second, always-stale copy of contracts it does not
--   own. A downstream projection that cares about one family reads this table
--   and owns that interpretation. Storing what was actually sent is the point:
--   this is a capture surface, not an aggregate.
--
--   Discovered + applied by scripts/run-projection-migrations.py (node-owned
--   migrations/ discovery).
--
-- ROW-LEVEL SECURITY IS NOT IN THIS FILE -- see 0002. It was, in the first
-- draft, and that made this a FORCE-RLS migration, which the forward-migration
-- runner refuses outright unless the id is in the operator fence
-- (docker/migrations/forward/fenced-node-migrations.yaml). Splitting it is not
-- a workaround: it is the shape every sibling already uses (create, then a
-- separate tenant-RLS file), and it is what lets the TABLE be created on every
-- lane while the RLS posture stays under the operator fence that the
-- platform-wide writer-proof programme governs.
--
-- Idempotent: CREATE TABLE / INDEX / TRIGGER are all guarded.

-- ============================================================================
-- HOOK_EVENTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS hook_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Tenant identity. TEXT holding the slug, matching every other landed
    -- tenant_id column on this surface (delegation 0022/0025, savings 080,
    -- registration 0002, skill_executions 0002) -- OMN-15356 converts that
    -- whole set to the canonical UUID in ONE pass. Adding a UUID column here
    -- alone would fork tenant identity inside a single database.
    tenant_id TEXT NOT NULL DEFAULT 'omninode',

    -- Durable dedupe key: sha256 over the canonical event body, computed by
    -- the submitter. NOT event_id -- see the header note.
    event_sha CHAR(64) NOT NULL,

    -- Producer's own type. Either a canonical ONEX topic-shaped name or a bare
    -- dotted name; both forms are present in the real corpus.
    event_type VARCHAR(200) NOT NULL,

    -- The producer's own timestamp where it had one, else its spool time.
    -- Never ingest time: these events are historical, and stamping them with
    -- ingest time would destroy the only ordering signal they carry.
    occurred_at TIMESTAMPTZ NOT NULL,

    -- Verbatim event body. Uninterpreted here by design.
    payload JSONB NOT NULL,

    -- Correlation metadata. All nullable: two of the four families carry no
    -- event_id whatsoever, and NULL here is the honest representation of
    -- "this producer does not emit one" rather than a fabricated value.
    event_id VARCHAR(64),
    correlation_id VARCHAR(64),
    run_id VARCHAR(64),

    -- Capture provenance.
    source VARCHAR(64) NOT NULL,
    batch_sha CHAR(64) NOT NULL,
    spooled_at TIMESTAMPTZ,
    -- Why the producer's local emit failed. Retained because discarding it
    -- destroys the only evidence of WHY these events were ever stranded.
    spool_reason TEXT,

    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_hook_events_tenant_event_sha UNIQUE (tenant_id, event_sha),
    CONSTRAINT hook_events_event_sha_is_sha256
        CHECK (event_sha ~ '^[0-9a-f]{64}$'),
    CONSTRAINT hook_events_batch_sha_is_sha256
        CHECK (batch_sha ~ '^[0-9a-f]{64}$'),
    CONSTRAINT hook_events_payload_is_object
        CHECK (jsonb_typeof(payload) = 'object')
);

-- ---- BEGIN OMN-15376 shape reconciliation: hook_events ----
-- CREATE TABLE IF NOT EXISTS silently NO-OPS when a table of this name already
-- exists with a DIFFERENT shape (an out-of-band or legacy apply). Everything
-- below is not so forgiving: CREATE INDEX IF NOT EXISTS guards the index NAME,
-- not the COLUMN, so the first column-dependent statement raises
--   ERROR: column "<col>" does not exist
-- and ON_ERROR_STOP=1 kills the whole migration Job there, one instance per
-- deploy cycle.
--
-- The guarded adds below converge a drifted pre-existing table onto the shape
-- declared above. On the fresh-create path every one is a no-op, so BOTH paths
-- end at the same schema. No DROP, no recreate, no TRUNCATE. A column that
-- cannot be made NOT NULL without inventing data fails LOUD and names the
-- exact conflict instead of guessing.

ALTER TABLE hook_events ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE hook_events ADD COLUMN IF NOT EXISTS tenant_id TEXT DEFAULT 'omninode';
ALTER TABLE hook_events ADD COLUMN IF NOT EXISTS event_sha CHAR(64);
ALTER TABLE hook_events ADD COLUMN IF NOT EXISTS event_type VARCHAR(200);
ALTER TABLE hook_events ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMPTZ;
ALTER TABLE hook_events ADD COLUMN IF NOT EXISTS payload JSONB;
ALTER TABLE hook_events ADD COLUMN IF NOT EXISTS event_id VARCHAR(64);
ALTER TABLE hook_events ADD COLUMN IF NOT EXISTS correlation_id VARCHAR(64);
ALTER TABLE hook_events ADD COLUMN IF NOT EXISTS run_id VARCHAR(64);
ALTER TABLE hook_events ADD COLUMN IF NOT EXISTS source VARCHAR(64);
ALTER TABLE hook_events ADD COLUMN IF NOT EXISTS batch_sha CHAR(64);
ALTER TABLE hook_events ADD COLUMN IF NOT EXISTS spooled_at TIMESTAMPTZ;
ALTER TABLE hook_events ADD COLUMN IF NOT EXISTS spool_reason TEXT;
ALTER TABLE hook_events ADD COLUMN IF NOT EXISTS captured_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE hook_events ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col   TEXT;
    v_nulls BIGINT;
BEGIN
    -- Only the columns the contract declares NOT NULL. event_id /
    -- correlation_id / run_id / spooled_at / spool_reason are deliberately
    -- absent: NULL is their honest value for producers that emit no such
    -- field, and forcing them NOT NULL would require inventing identity.
    FOREACH v_col IN ARRAY ARRAY['id', 'tenant_id', 'event_sha', 'event_type', 'occurred_at', 'payload', 'source', 'batch_sha', 'captured_at', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'hook_events'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'hook_events'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-16090: cannot converge hook_events.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'hook_events'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE hook_events ADD CONSTRAINT hook_events_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'hook_events'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['event_sha', 'tenant_id']::text[]
    ) THEN
        ALTER TABLE hook_events ADD CONSTRAINT uq_hook_events_tenant_event_sha UNIQUE (tenant_id, event_sha);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'hook_events'::regclass AND conname = 'hook_events_event_sha_is_sha256'
    ) THEN
        ALTER TABLE hook_events ADD CONSTRAINT hook_events_event_sha_is_sha256 CHECK (event_sha ~ '^[0-9a-f]{64}$');
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'hook_events'::regclass AND conname = 'hook_events_batch_sha_is_sha256'
    ) THEN
        ALTER TABLE hook_events ADD CONSTRAINT hook_events_batch_sha_is_sha256 CHECK (batch_sha ~ '^[0-9a-f]{64}$');
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'hook_events'::regclass AND conname = 'hook_events_payload_is_object'
    ) THEN
        ALTER TABLE hook_events ADD CONSTRAINT hook_events_payload_is_object CHECK (jsonb_typeof(payload) = 'object');
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: hook_events ----


CREATE INDEX IF NOT EXISTS idx_hook_events_tenant_id
    ON hook_events (tenant_id);

CREATE INDEX IF NOT EXISTS idx_hook_events_event_type_occurred_at
    ON hook_events (event_type, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_hook_events_occurred_at
    ON hook_events (occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_hook_events_correlation_id
    ON hook_events (correlation_id)
    WHERE correlation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_hook_events_run_id
    ON hook_events (run_id)
    WHERE run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_hook_events_batch_sha
    ON hook_events (batch_sha);

-- ============================================================================
-- TRIGGER: auto-update updated_at
-- ============================================================================
CREATE OR REPLACE FUNCTION update_hook_events_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_hook_events_updated_at ON hook_events;
CREATE TRIGGER trigger_hook_events_updated_at
    BEFORE UPDATE ON hook_events
    FOR EACH ROW
    EXECUTE FUNCTION update_hook_events_updated_at();
