-- =============================================================================
-- MIGRATION: physically deliver log_entries through the node-owned migration
--            loop (OMN-15846)
-- =============================================================================
-- Ticket: OMN-15846 (cross-DB flat migration classification audit — 083/096/
--         097 census amendment; 083 is genuinely undelivered and deliverable)
-- Related: OMN-12131 (original ticket for the flat migration this file
--          replaces the DELIVERY of —
--          docker/migrations/forward/083_create_log_entries.sql in
--          omnibase_infra), OMN-15819 (the precedent this file follows:
--          node-owned replacement for a flat migration the k8s Job cannot
--          reach), OMN-15282/OMN-15313 (node-owned migration discovery loop
--          this file runs through), OMN-15359/OMN-15423 (omninode_internal
--          domain classification this file's schema qualification follows)
-- Version: 1.0.0
--
-- WHY THIS FILE EXISTS (OMN-15846)
--   docker/migrations/forward/083_create_log_entries.sql (omnibase_infra)
--   carries `\connect omnidash_analytics` and is a FLAT migration
--   (docker/migrations/forward/*.sql, -maxdepth 1). The k8s Job that applies
--   that corpus (omninode_infra repo,
--   k8s/migrations/omnibase-infra-migrate.yaml) owns exactly one database,
--   omnibase_infra — its flat loop's `psql -f` apply is gated on
--   `directive_db == $DB_NAME` and is UNREACHABLE for a cross-DB file, in
--   that loop or any other in the runner (same OMN-15819 defect class as
--   098/099). 083 carried a false "applied" ledger row in omnibase_infra's
--   own schema_migrations (applied_at 2026-07-28T22:53:47Z, checksum
--   byte-identical to the live flat file) that masked this until the
--   OMN-15846 classification-ordering fix unmasked it. Live-confirmed
--   2026-08-10 (role_omnidash / omninode_runtime, omninode-dev-postgres
--   RDS): `to_regclass('public.log_entries')` is NULL in BOTH
--   omnibase_infra and omnidash_analytics — the table has never existed on
--   either database on this lane.
--
--   THIS file is a NODE-OWNED migration, vendored into omnibase_infra under
--   docker/migrations/forward/nodes/node_log_persistence_effect/. The
--   node-owned loop in the SAME k8s Job (OMN-15282/OMN-15313) connects
--   DIRECTLY to omnidash_analytics as role_omnidash — it is the one code
--   path in the whole runner that can actually reach this database. That is
--   the entire fix: relocate delivery, do not relocate intent. 083 itself
--   stays in place in omnibase_infra (byte-unchanged except for a header
--   tombstone comment, OMN-15846) as the ledgered historical record of the
--   original design.
--
-- WHY SCHEMA-QUALIFIED omninode_internal.log_entries, NOT BARE log_entries
--   083's original design created the table unqualified (implicit `public`
--   in the omnidash_analytics session). Because this table has never
--   physically existed anywhere (see above), there is no legacy `public`
--   row set to reconcile against — this is a first physical creation, not a
--   cutover. `scripts/ci/check_application_database_sql.py`
--   (OMN-15361/OMN-15423 domain enforcement) requires every NEW deployable
--   SQL target to be schema-qualified against a declared topology domain;
--   `omninode_internal` matches this node's own `db_io.db_tables[].schema`
--   declaration in contract.yaml and the same domain node_service_registry,
--   node_projection_live_events, and every other OMNINODE_INTERNAL-domain
--   node migration in this corpus already uses. role_omnidash — the
--   identity the node-owned loop connects as — was live-confirmed
--   2026-08-10 to already hold USAGE and CREATE on schema
--   `omninode_internal` in omnidash_analytics (the OMN-15819 step-3
--   operator grant has since landed), so this file can create directly
--   under it with no additional grant required.
--
-- WHY role_omnidash NEEDS NO EXPLICIT GRANT HERE
--   role_omnidash owns the `omninode_internal` schema's CREATE privilege
--   (see above) and, as the connecting/creating identity, becomes the table
--   owner automatically with full implicit privileges. No GRANT statement
--   is required or issued.
--
-- CONSUMER (currently dormant on onex-dev, tracked separately)
--   omnimarket.nodes.node_log_persistence_effect INSERTs into this table on
--   `onex.evt.platform.log-entry.v1`. On onex-dev today the handler's
--   `ONEX_PG_DSN` is unset (no wiring found in
--   omninode_infra/k8s/onex-dev/runtime/*.yaml) — the handler's own
--   documented graceful-degradation path ("no DB pool available") applies,
--   so no write is attempted either way. This migration closes the schema
--   gap; the DSN-wiring gap that keeps the feature end-to-end dormant is a
--   separate, already-filed concern (see OMN-14153 for the sibling .201
--   dev-lane symptom of the same feature family) and is not this file's
--   scope. The handler's own `INSERT INTO log_entries` (unqualified) will
--   need a matching schema-qualification follow-up when that DSN-wiring
--   ticket is picked up — out of scope here, noted for that ticket.
--
-- IDEMPOTENCY
--   CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS are safe to
--   re-run. No role or grant DDL appears in this file.
--
-- ROLLBACK
--   DROP TABLE omninode_internal.log_entries. Safe: the table is created
--   empty by this file on every path that executes it, and nothing else in
--   the corpus references it.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.log_entries (
    entry_id        UUID            PRIMARY KEY,
    timestamp       TIMESTAMPTZ     NOT NULL,
    node_name       TEXT            NOT NULL,
    function_name   TEXT            NOT NULL DEFAULT '',
    level           TEXT            NOT NULL DEFAULT 'info',
    message         TEXT            NOT NULL,
    correlation_id  TEXT,
    duration_ms     DOUBLE PRECISION,
    metadata        JSONB           NOT NULL DEFAULT '{}',
    ingested_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: omninode_internal.log_entries ----
-- CREATE TABLE IF NOT EXISTS silently no-ops against a drifted pre-existing
-- table; the guarded adds below converge such a table onto the shape
-- declared above (no-ops on the fresh-create path, since every column
-- already exists there). No DROP, no recreate, no TRUNCATE.
ALTER TABLE omninode_internal.log_entries ADD COLUMN IF NOT EXISTS entry_id UUID;
ALTER TABLE omninode_internal.log_entries ADD COLUMN IF NOT EXISTS timestamp TIMESTAMPTZ;
ALTER TABLE omninode_internal.log_entries ADD COLUMN IF NOT EXISTS node_name TEXT;
ALTER TABLE omninode_internal.log_entries ADD COLUMN IF NOT EXISTS function_name TEXT DEFAULT '';
ALTER TABLE omninode_internal.log_entries ADD COLUMN IF NOT EXISTS level TEXT DEFAULT 'info';
ALTER TABLE omninode_internal.log_entries ADD COLUMN IF NOT EXISTS message TEXT;
ALTER TABLE omninode_internal.log_entries ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE omninode_internal.log_entries ADD COLUMN IF NOT EXISTS duration_ms DOUBLE PRECISION;
ALTER TABLE omninode_internal.log_entries ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}';
ALTER TABLE omninode_internal.log_entries ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ DEFAULT NOW();
-- ---- END OMN-15376 shape reconciliation: omninode_internal.log_entries ----

COMMENT ON TABLE omninode_internal.log_entries IS
    'Structured log events from ONEX nodes (OMN-12131). '
    '30-day retention window anchored on ingested_at. '
    'Consumed by node_log_persistence_effect via onex.evt.*.log-emitted.v1.';

COMMENT ON COLUMN omninode_internal.log_entries.entry_id IS
    'Client-supplied UUID — idempotency key for exactly-once ingestion.';

COMMENT ON COLUMN omninode_internal.log_entries.ingested_at IS
    '30-day retention anchor. Rows with ingested_at < NOW() - INTERVAL ''30 days'' '
    'are eligible for pruning by the nightly retention job.';

-- Partial index: correlation lookups (skip NULL rows to reduce index size)
CREATE INDEX IF NOT EXISTS idx_log_entries_correlation
    ON omninode_internal.log_entries (correlation_id)
    WHERE correlation_id IS NOT NULL;

-- Composite: node-scoped time-range queries (dashboard primary query path)
CREATE INDEX IF NOT EXISTS idx_log_entries_node_ts
    ON omninode_internal.log_entries (node_name, timestamp DESC);

-- Composite: level-filtered time-range queries (error/warn filtering)
CREATE INDEX IF NOT EXISTS idx_log_entries_level_ts
    ON omninode_internal.log_entries (level, timestamp DESC);

-- Standalone timestamp: full time-range scans and retention sweeps
CREATE INDEX IF NOT EXISTS idx_log_entries_ts
    ON omninode_internal.log_entries (timestamp DESC);
