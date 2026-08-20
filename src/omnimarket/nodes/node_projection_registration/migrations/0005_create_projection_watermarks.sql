-- OMN-16146: create the missing projection_watermarks table.
--
-- WHY THIS EXISTS
--   omnimarket.projection.runner.BaseProjectionRunner._update_watermark()
--   upserts into projection_watermarks after every committed batch, for
--   EVERY projection writer node (registration, llm-cost, savings,
--   delegation, baselines, routing-decision, session-outcome, ...) -- see
--   docker-compose.projection.yml, all seven services share one
--   OMNIDASH_ANALYTICS_DB_URL database. The table was never migrated in
--   this repo (grep across git history finds no prior
--   "CREATE TABLE ... projection_watermarks" here), so on .201 dev Postgres
--   the writer logged "Failed to update watermark: relation
--   projection_watermarks does not exist" on every batch. Projection rows
--   still landed (the INSERT is wrapped in a try/except that only warns),
--   but watermark persistence was silently dead, so a writer restart could
--   not resume from a persisted watermark.
--
--   This is one physical table in the shared DB, not a per-node schema, so
--   one migration -- owned here by node_projection_registration, the writer
--   named in OMN-16146 -- creates it once for every projection writer that
--   depends on it.
--
--   Column set and the ON CONFLICT (projection_name) target are taken
--   verbatim from BaseProjectionRunner._update_watermark's INSERT statement
--   (src/omnimarket/projection/runner.py), which this migration's companion
--   commit schema-qualifies to omninode_internal.projection_watermarks in
--   lockstep with this file:
--     INSERT INTO omninode_internal.projection_watermarks
--       (projection_name, last_offset, events_projected, updated_at)
--     VALUES ($1, $2, 1, NOW())
--     ON CONFLICT (projection_name) DO UPDATE SET
--       last_offset = GREATEST(projection_watermarks.last_offset, EXCLUDED.last_offset),
--       events_projected = projection_watermarks.events_projected + 1,
--       last_projected_at = NOW(), updated_at = NOW()
--
-- WHY SCHEMA-QUALIFIED omninode_internal.projection_watermarks, NOT BARE
--   This table has never physically existed anywhere (no prior CREATE TABLE
--   for it in this corpus) -- this is a first physical creation, not a
--   cutover, so there is no legacy public row set to reconcile against.
--   scripts/ci/check_application_database_sql.py (OMN-15361/OMN-15423
--   domain enforcement) requires every NEW deployable SQL target to be
--   schema-qualified against a declared topology domain; omninode_internal
--   matches this node's own db_io.db_tables[].schema declaration in
--   contract.yaml and the same domain node_service_registry and every
--   other OMNINODE_INTERNAL-domain node migration in this corpus already
--   uses (e.g. node_log_persistence_effect/0000_create_log_entries.sql,
--   OMN-15846 -- this file follows that precedent).
--
-- WHY THIS FILE ALSO GRANTS omninode_runtime (topology-derived, OMN-16146)
--   src/omnibase_infra/topology/instances/*.yaml declares
--   principals.omninode_runtime.grants[schema: omninode_internal] for this
--   table (regenerated in the paired omnibase_infra PR via
--   scripts/generate_application_database_table_grants.py --write) --
--   INSERT/SELECT/UPDATE only, matching every other projection-writer
--   table's invariant (a projection writer upserts, it does not reshape
--   the table).
--
-- WHY NO GUARDED DO $$ ... $$ ROLE-CREATION BLOCK (unlike some sibling
-- omninode_internal migrations, e.g. 099/log_entries)
--   scripts/ci/check_application_database_sql.py unconditionally rejects
--   ANY DO $$ ... $$ procedural block in a newly-linted file as "dynamic
--   SQL whose relation targets cannot be proven statically" -- verified
--   directly against this repo's own linter (log_entries and 099 would
--   ALSO fail this rule if freshly linted today; they only escape because
--   the OMN-15361 gate only lints files CHANGED in a given PR's diff, and
--   neither has been touched since that rule was added). This is a
--   node-owned migration under docker/migrations/forward/nodes/, which
--   scripts/run-migrations.py's Migration Integration Test CI job (bare
--   postgres:16-alpine) discovers via migration_dir.glob("*.sql")
--   (non-recursive) and therefore never applies -- the guard's original
--   justification (surviving that bare-Postgres CI scope) does not apply
--   to a node-owned file. Plain GRANT below: fails loud with `role
--   "omninode_runtime" does not exist` if run somewhere the role genuinely
--   is not yet provisioned, per root CLAUDE.md's fail-fast/no-defensive-
--   defaults rule, rather than swallowing that gap silently.
--
-- Idempotency: CREATE TABLE / INDEX are guarded so the migration is safe on
-- a DB where the table already exists and on a fresh omnidash_analytics.
-- The GRANT below is idempotent by Postgres's own semantics (re-granting an
-- already-held privilege is a no-op, not an error).

CREATE SCHEMA IF NOT EXISTS omninode_internal;

CREATE TABLE IF NOT EXISTS omninode_internal.projection_watermarks (
    projection_name TEXT PRIMARY KEY,

    last_offset BIGINT NOT NULL DEFAULT 0,
    events_projected BIGINT NOT NULL DEFAULT 0,

    last_projected_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT non_negative_projection_watermarks_last_offset CHECK (last_offset >= 0),
    CONSTRAINT non_negative_projection_watermarks_events_projected CHECK (events_projected >= 0)
);

-- ---- BEGIN OMN-15376 shape reconciliation: omninode_internal.projection_watermarks ----
-- CREATE TABLE IF NOT EXISTS silently no-ops against a drifted pre-existing
-- table; the guarded adds below converge such a table onto the shape
-- declared above (no-ops on the fresh-create path, since every column
-- already exists there). No DROP, no recreate, no TRUNCATE. Matches
-- node_log_persistence_effect/0000_create_log_entries.sql's own precedent.
ALTER TABLE omninode_internal.projection_watermarks ADD COLUMN IF NOT EXISTS projection_name TEXT;
ALTER TABLE omninode_internal.projection_watermarks ADD COLUMN IF NOT EXISTS last_offset BIGINT DEFAULT 0;
ALTER TABLE omninode_internal.projection_watermarks ADD COLUMN IF NOT EXISTS events_projected BIGINT DEFAULT 0;
ALTER TABLE omninode_internal.projection_watermarks ADD COLUMN IF NOT EXISTS last_projected_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.projection_watermarks ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.projection_watermarks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- Defaults: ADD COLUMN IF NOT EXISTS ... DEFAULT is a no-op on a column that
-- already existed without one -- restore the declared defaults explicitly so
-- a drifted pre-existing column converges too, not only a brand-new one.
ALTER TABLE omninode_internal.projection_watermarks ALTER COLUMN last_offset SET DEFAULT 0;
ALTER TABLE omninode_internal.projection_watermarks ALTER COLUMN events_projected SET DEFAULT 0;
ALTER TABLE omninode_internal.projection_watermarks ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE omninode_internal.projection_watermarks ALTER COLUMN updated_at SET DEFAULT NOW();

-- No NOT NULL/PRIMARY KEY convergence block here (unlike log_entries'
-- precedent): this table has never physically existed anywhere (see the
-- file header), so there is no pre-existing drifted-shape row set to
-- reconcile against -- the CREATE TABLE above already declares
-- projection_name PRIMARY KEY and the NOT NULL columns directly, which is
-- sufficient on a genuine fresh-create path. A DO $$ ... $$ convergence
-- block would also unconditionally trip
-- scripts/ci/check_application_database_sql.py's dynamic-SQL rejection for
-- a newly-linted file (see above) with no compensating benefit here.
-- ---- END OMN-15376 shape reconciliation: omninode_internal.projection_watermarks ----

CREATE INDEX IF NOT EXISTS idx_projection_watermarks_updated_at
    ON omninode_internal.projection_watermarks (updated_at DESC);

-- -----------------------------------------------------------------------------
-- omninode_runtime grant (topology-derived, OMN-16146)
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;
GRANT SELECT, INSERT, UPDATE ON omninode_internal.projection_watermarks TO omninode_runtime;
