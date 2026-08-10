-- =============================================================================
-- MIGRATION: physically deliver omninode_internal.live_events through the
--            node-owned migration loop (OMN-15819)
-- =============================================================================
-- Ticket: OMN-15819 (migration runner skip-ledgers cross-DB \connect files
--         without executing them -- 098/099 recorded "applied" in the wrong
--         database while omninode_internal.live_events was never created;
--         no code path can deliver them)
-- Related: OMN-15359 (099_create_omninode_internal_live_events.sql -- the
--          flat migration this file replaces the DELIVERY of),
--          OMN-15282 (node-owned migration discovery loop this file runs
--          through), OMN-13079 (0000_create_live_events.sql -- the DDL owner
--          of public.live_events, the table this schema mirrors)
-- Version: 1.0.0
--
-- WHY THIS FILE EXISTS (OMN-15819 root cause)
--   docker/migrations/forward/099_create_omninode_internal_live_events.sql
--   carries `\connect omnidash_analytics` and is a FLAT migration
--   (docker/migrations/forward/*.sql, -maxdepth 1). The k8s Job that applies
--   that corpus (omninode_infra repo,
--   k8s/migrations/omnibase-infra-migrate.yaml) owns exactly one database,
--   omnibase_infra -- its flat loop's `psql -f` apply is gated on
--   `directive_db == $DB_NAME` and is UNREACHABLE for a cross-DB file, in
--   that loop or any other in the runner. 099 has therefore never executed
--   anywhere: omnidash_analytics never saw it, and
--   `omninode_internal.live_events` was never created, live-confirmed via a
--   fresh read-only probe on the exact database this file targets
--   (2026-08-10, role_omnidash, omninode-dev-postgres RDS): `to_regclass`
--   fails closed with `permission denied for schema omninode_internal`
--   (role_omnidash has neither USAGE nor CREATE there today), and the
--   runtime write path (handler_wiring._resolve_projection_database_target,
--   which has issued `INSERT INTO omninode_internal.live_events` since
--   before 099 merged) fails every write with UndefinedTable, at ~6/min
--   paired with a DLQ event on the -effects pod.
--
--   THIS file is a NODE-OWNED migration, vendored under
--   docker/migrations/forward/nodes/node_projection_live_events/. The
--   node-owned loop in the SAME k8s Job (OMN-15282/OMN-15313) connects
--   DIRECTLY to omnidash_analytics as role_omnidash -- it is the one code
--   path in the whole runner that can actually reach this database. That is
--   the entire fix: relocate delivery, do not relocate intent. 099 itself
--   stays in place (byte-unchanged except for a header tombstone comment,
--   OMN-15819 step 1/2) as the ledgered historical record of the original
--   design; its own transform-copy/reconciliation logic for
--   PRE-EXISTING public.live_events rows is intentionally NOT duplicated
--   here -- this file's scope is closing the UndefinedTable write-path gap,
--   not backfilling history (a distinct, separately-scoped follow-up if the
--   dashboard needs continuous history in the internal-schema copy).
--
-- THE SCHEMA TRAP THIS FILE ASSERTS, NOT WORKS AROUND
--   omninode_internal EXISTS in omnidash_analytics today (created
--   out-of-band, owner `omninodeadmin` master; live-confirmed present via
--   pg_namespace, which needs no schema-level privilege to read) but
--   role_omnidash -- the role THIS loop connects as -- has neither USAGE
--   nor CREATE on it (both live-confirmed `false` via has_schema_privilege,
--   2026-08-10). Only the schema OWNER (or a role with GRANT OPTION) can
--   grant those, so role_omnidash cannot self-grant its way in. The
--   one-time repair is an OPERATOR action (OMN-15819 step 3, queued,
--   out-of-band, ~30s as omninodeadmin master):
--     GRANT USAGE, CREATE ON SCHEMA omninode_internal TO role_omnidash
--       WITH GRANT OPTION;
--   WITH GRANT OPTION corrects the ticket's originally-stated recipe (plain
--   GRANT, no grant option) -- live-proven while authoring this file that
--   plain USAGE lets role_omnidash create the table but leaves its OWN
--   onward `GRANT USAGE ... TO omninode_runtime` (step 5 below) a silent
--   no-op (`WARNING: no privileges were granted`), which would leave the
--   runtime write path broken while this migration reports success. See
--   precondition 2 below for the live citation.
--   This migration does NOT attempt that grant itself -- it has no rights to
--   issue it and mis-designing around that (e.g. self-escalating privilege)
--   is explicitly out of scope. Instead it ASSERTS the precondition
--   fail-fast, by design: a migration that silently no-ops or half-applies
--   against a schema it cannot use is a worse failure mode than a named,
--   loud refusal.
--
-- WHY EVERY PRECONDITION IS A BARE SELECT, NOT A DO/RAISE BLOCK
--   The application-database SQL gate
--   (src/omnibase_infra/validation/application_database_domain_enforcement.py
--   `_requires_dynamic_sql_rejection`) rejects ANY new `DO $$ ... $$` block
--   in a migration touching an application-topology schema, unconditionally
--   -- procedural execution cannot be proven to target only statically-known
--   relations, so it is refused outright rather than inspected case-by-case.
--   098/099 (both DO-block-heavy) predate this gate's diff scope and were
--   never re-scanned against it. This file has no such grandfathering, so
--   every precondition below uses the same statically-provable
--   `SELECT 1 / count(*)` division-by-zero idiom 098/099 already use for
--   their own POST-conditions (OMN-15361) -- division by zero is Postgres's
--   own fail-closed primitive when the guarded condition is false, and it
--   needs no procedural block to express. The tradeoff is a generic
--   `division by zero` error instead of a hand-authored message naming the
--   exact OMN-15819 remedy; that remedy is documented here and on the
--   ticket instead of in the error text itself.
--
--   The same constraint retired the omninode_runtime guard-created-if-absent
--   DO block 099 uses (CREATE ROLE has no IF NOT EXISTS form in Postgres, so
--   idempotent creation is a DO-block-only idiom). Provisioning
--   omninode_runtime is not this migration's concern any more than
--   provisioning omninode_internal itself is (see the schema trap above) --
--   the role already exists on the live target (pg_roles, 2026-08-10) and is
--   now asserted, not created, exactly like the schema and privilege
--   preconditions above it.
--
-- IDEMPOTENCY
--   CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS are safe to
--   re-run. Every precondition is a read-only catalog probe -- re-run is a
--   no-op once the operator grant has landed. No ALTER of any kind (other
--   than the guarded ADD COLUMN IF NOT EXISTS reconciliation block) appears
--   in this file.
--
-- CANONICAL SHAPE (do not hand-retype, OMN-15384 shape-parity gate)
--   The CREATE TABLE body below is copied VERBATIM from
--   docker/migrations/forward/099_create_omninode_internal_live_events.sql
--   (the flat migration this file replaces the delivery of), which is
--   itself already ledgered `status: identical` in
--   docker/migrations/forward/flat-node-shape-parity.yaml against this
--   node's own bare `live_events`
--   (nodes/node_projection_live_events/0000_create_live_events.sql). Keeping
--   the two declarations byte-identical (modulo the `omninode_internal.`
--   qualifier) means this file introduces no NEW shape for that gate to
--   track -- the live overlap set and ledger are unchanged by this PR
--   (verified: tests/ci/test_flat_node_migration_shape_parity.py passes
--   unmodified).
--
-- WHY THE omninode_runtime GRANT (runtime-owns-DB doctrine)
--   handler_wiring._resolve_projection_database_target issues
--   `INSERT INTO omninode_internal.live_events` under the omninode_runtime
--   binding -- the same write-path grant 099 itself carries (SELECT,
--   INSERT, UPDATE; no DELETE -- a projection writer upserts, it does not
--   reshape the table, matching 096's role_omnidash invariant and 099's own
--   omninode_runtime invariant).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Precondition: the schema must exist at all. Statically provable (no
--    DO/RAISE -- see the file-header rationale above): division by zero
--    when the schema is absent. pg_namespace needs no schema-level
--    privilege to read, so this probe is safe under any connecting role.
--    has_schema_privilege() itself RAISES (not a false/NULL result) for a
--    schema name that does not exist, which is why this check runs FIRST,
--    strictly before precondition 2 below ever calls it.
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS omninode_internal_schema_exists_precondition
  FROM pg_catalog.pg_namespace
 WHERE nspname = 'omninode_internal';

-- -----------------------------------------------------------------------------
-- 2. Precondition: CURRENT_USER (whichever role is actually executing this
--    file) must hold USAGE, CREATE, and USAGE WITH GRANT OPTION on
--    omninode_internal. Checked against current_user, not a hardcoded role
--    name, because the connecting identity differs by lane: role_omnidash
--    on the k8s Job / managed RDS lane (OMN-15313 NODE_DB_USER), but the
--    `postgres` superuser on the compose lane (POSTGRES_USER default, no
--    per-role split there -- see 096's own docstring for the live readback
--    proving this). A superuser always reads back `true` for every
--    has_schema_privilege() check regardless of ACL rows, so this precondition
--    is a no-op on compose and a real, live-confirmed-false gate on RDS
--    today (role_omnidash has neither USAGE nor CREATE, 2026-08-10).
--
--    USAGE WITH GRANT OPTION is a SEPARATE requirement from plain USAGE:
--    this migration re-grants schema USAGE onward to omninode_runtime
--    (step 5 below), and Postgres requires the granting role to hold grant
--    option for that specific privilege, not merely the privilege itself,
--    or the onward GRANT silently no-ops with `WARNING: no privileges were
--    granted for "omninode_internal"` -- proven live in an ephemeral
--    sandbox while authoring this file: a plain (non-grant-option) USAGE
--    grant to role_omnidash let this migration create the table and grant
--    TABLE-level privileges (ownership of a self-created object carries its
--    own grant rights), but the SCHEMA-level forward to omninode_runtime
--    silently granted nothing, which would have left the runtime write path
--    just as broken as before this migration (permission denied for
--    schema, not UndefinedTable) while the migration itself reported
--    success. This corrects the ticket's originally-stated operator recipe
--    (OMN-15819 step 3), which did not specify WITH GRANT OPTION --
--    flagged on the PR and on the ticket.
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS omninode_internal_schema_privilege_precondition
  FROM (
    SELECT 1
     WHERE has_schema_privilege(current_user, 'omninode_internal', 'USAGE')
       AND has_schema_privilege(current_user, 'omninode_internal', 'CREATE')
       AND has_schema_privilege(
             current_user, 'omninode_internal', 'USAGE WITH GRANT OPTION'
           )
  ) AS assertion;

-- -----------------------------------------------------------------------------
-- 3. Precondition: omninode_runtime must already exist. Provisioning it is
--    not this migration's concern (see the file-header rationale above) --
--    the role already exists on the live target (pg_roles, 2026-08-10) and
--    is asserted, not created.
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS omninode_runtime_role_exists_precondition
  FROM pg_catalog.pg_roles
 WHERE rolname = 'omninode_runtime';

-- -----------------------------------------------------------------------------
-- 4. Canonical 10-column shape, copied verbatim from
--    docker/migrations/forward/099_create_omninode_internal_live_events.sql
--    -- see the file-header rationale above. Do not hand-retype; if the
--    shape ever needs to change, change 099 first and copy forward again.
--
--    No CREATE EXTENSION pgcrypto here (unlike 099): this node's own
--    0000_create_live_events.sql already issues
--    `CREATE EXTENSION IF NOT EXISTS pgcrypto` (unqualified) and runs
--    strictly before this file in every scope that applies node migrations
--    at all (same node directory, sorted lexical order, same database) --
--    the extension is a database-level object that persists across the
--    separate psql connection each file gets, so it is already installed
--    and reachable via the default search_path by the time this file runs.
--    A second, redundant CREATE EXTENSION here would also need an explicit
--    `WITH SCHEMA <application-schema>` target (application-database SQL
--    gate, tests/unit/validation/test_application_database_domain_enforcement.py)
--    and a matching schema-qualified gen_random_uuid() call below --
--    strictly worse than relying on the guarantee 0000 already provides.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS omninode_internal.live_events (
  id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id       TEXT        UNIQUE NOT NULL,
  type           TEXT        NOT NULL DEFAULT 'ACTION',
  timestamp      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  source         TEXT        NOT NULL DEFAULT 'platform',
  topic          TEXT        NOT NULL DEFAULT '',
  summary        TEXT        NOT NULL DEFAULT '',
  payload        TEXT        NOT NULL DEFAULT '{}',
  correlation_id TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: omninode_internal.live_events ----
-- CREATE TABLE IF NOT EXISTS above silently no-ops if this table already
-- exists with a different shape (schema-parity gate,
-- tests/ci/test_node_migration_shape_reconciliation.py). The schema was
-- live-confirmed EMPTY of tables at the time this file was authored
-- (2026-08-10, out-of-band creation carried only the schema itself), so this
-- is a mechanical compliance guard, not a response to observed drift -- every
-- add is a no-op on the fresh-create path this migration actually exercises.
--
-- WHAT THIS BLOCK DOES NOT DO (OMN-15819 CodeRabbit thread r3749990744): a
-- column-level `ADD COLUMN IF NOT EXISTS` can only ever add a NULLABLE
-- column -- it cannot retroactively apply the PRIMARY KEY, the `event_id`
-- UNIQUE constraint, or any NOT NULL from the CREATE TABLE above onto a
-- pre-existing, non-canonical table, and a DO block that attempted that
-- reconciliation is exactly the procedural-execution shape the
-- application-database SQL gate rejects (see the file-header rationale).
-- Rather than silently leaving a shape-degraded table able to accept
-- duplicate/incomplete events while this migration reports success, section
-- 7 below adds statically-provable post-conditions asserting the PRIMARY
-- KEY, the `event_id` UNIQUE constraint, and every required-column NOT NULL
-- are actually present after this file runs -- on a pre-existing table
-- missing any of them, the migration now fails loudly instead of no-opping.
ALTER TABLE omninode_internal.live_events
  ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE omninode_internal.live_events
  ADD COLUMN IF NOT EXISTS event_id TEXT;
ALTER TABLE omninode_internal.live_events
  ADD COLUMN IF NOT EXISTS type TEXT DEFAULT 'ACTION';
ALTER TABLE omninode_internal.live_events
  ADD COLUMN IF NOT EXISTS timestamp TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.live_events
  ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'platform';
ALTER TABLE omninode_internal.live_events
  ADD COLUMN IF NOT EXISTS topic TEXT DEFAULT '';
ALTER TABLE omninode_internal.live_events
  ADD COLUMN IF NOT EXISTS summary TEXT DEFAULT '';
ALTER TABLE omninode_internal.live_events
  ADD COLUMN IF NOT EXISTS payload TEXT DEFAULT '{}';
ALTER TABLE omninode_internal.live_events
  ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE omninode_internal.live_events
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
-- ---- END OMN-15376 shape reconciliation: omninode_internal.live_events ----

-- -----------------------------------------------------------------------------
-- 5. Indexes matching public.live_events's shape
--    (nodes/node_projection_live_events/0000_create_live_events.sql) and
--    099's own declaration for the omninode_internal copy.
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_omninode_internal_live_events_created_at
  ON omninode_internal.live_events (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_omninode_internal_live_events_topic
  ON omninode_internal.live_events (topic);

CREATE INDEX IF NOT EXISTS idx_omninode_internal_live_events_source
  ON omninode_internal.live_events (source);

CREATE INDEX IF NOT EXISTS idx_omninode_internal_live_events_correlation_id
  ON omninode_internal.live_events (correlation_id)
  WHERE correlation_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 6. Shape post-conditions -- BEFORE any GRANT below, deliberately (OMN-15819
--    CodeRabbit thread r3751589783). run-forward-migrations.sh invokes
--    `psql -v ON_ERROR_STOP=1 -f` with no --single-transaction and no
--    explicit BEGIN wrapping this file, so every statement here autocommits
--    independently as psql executes it. If these assertions ran AFTER the
--    GRANT (as in an earlier revision of this file), a failed assertion
--    would still leave the runtime write grant committed on a table this
--    migration just rejected. Ordering the grant last means ON_ERROR_STOP
--    aborts the script -- and grants nothing -- before any GRANT statement
--    is reached.
--
--    Statically provable (no DO/RAISE), matching the OMN-15361
--    application-database gate's requirement for deployable SQL and
--    098/099's own convention. The constraint-shape checks close the
--    reconciliation gap the ADD COLUMN IF NOT EXISTS block above cannot:
--    it can only add NULLABLE columns, never retroactively apply a PRIMARY
--    KEY, a UNIQUE constraint, or a NOT NULL onto a pre-existing,
--    non-canonical table (OMN-15819 CodeRabbit thread r3749990744). Each
--    check asserts the EXACT column set, not merely "some constraint of
--    this type exists" (OMN-15819 CodeRabbit thread r3751589796) -- a
--    pre-existing table with e.g. PRIMARY KEY on the wrong column, or a
--    composite UNIQUE(event_id, source) instead of UNIQUE(event_id), would
--    silently pass a looser check while still allowing duplicate event_id
--    values or rejecting the canonical row shape.
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS omninode_internal_live_events_exists_assertion
  FROM information_schema.tables
 WHERE table_schema = 'omninode_internal' AND table_name = 'live_events';

SELECT 1 / count(*) AS omninode_internal_live_events_primary_key_assertion
  FROM (
    SELECT tc.constraint_name
      FROM information_schema.table_constraints tc
      JOIN information_schema.key_column_usage kcu
        ON kcu.constraint_name = tc.constraint_name
       AND kcu.table_schema = tc.table_schema
     WHERE tc.table_schema = 'omninode_internal'
       AND tc.table_name = 'live_events'
       AND tc.constraint_type = 'PRIMARY KEY'
     GROUP BY tc.constraint_name
    HAVING count(*) = 1 AND bool_and(kcu.column_name = 'id')
  ) AS assertion;

SELECT 1 / count(*) AS omninode_internal_live_events_event_id_unique_assertion
  FROM (
    SELECT tc.constraint_name
      FROM information_schema.table_constraints tc
      JOIN information_schema.key_column_usage kcu
        ON kcu.constraint_name = tc.constraint_name
       AND kcu.table_schema = tc.table_schema
     WHERE tc.table_schema = 'omninode_internal'
       AND tc.table_name = 'live_events'
       AND tc.constraint_type = 'UNIQUE'
     GROUP BY tc.constraint_name
    HAVING count(*) = 1 AND bool_and(kcu.column_name = 'event_id')
  ) AS assertion;

SELECT 1 / count(*) AS omninode_internal_live_events_id_type_and_default_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'live_events'
   AND column_name = 'id'
   AND data_type = 'uuid'
   AND column_default IS NOT NULL;

SELECT 1 / count(*) AS omninode_internal_live_events_not_null_columns_assertion
  FROM (
    SELECT 1
     WHERE (
       SELECT count(*)
         FROM information_schema.columns
        WHERE table_schema = 'omninode_internal'
          AND table_name = 'live_events'
          AND column_name IN (
                'event_id', 'type', 'timestamp', 'source', 'topic',
                'summary', 'payload', 'created_at'
              )
          AND is_nullable = 'NO'
     ) = 8
  ) AS assertion;

-- -----------------------------------------------------------------------------
-- 7. Runtime-owns-DB doctrine: omninode_runtime read/write grant, identical
--    scope to 099's own (SELECT, INSERT, UPDATE -- no DELETE). Deliberately
--    last -- see section 6's header for why.
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;
GRANT SELECT, INSERT, UPDATE ON omninode_internal.live_events TO omninode_runtime;

COMMENT ON TABLE omninode_internal.live_events IS
  'Platform-wide bus event projection (internal-schema copy) -- delivered by '
  'the node-owned migration loop per OMN-15819; feeds the omnidash '
  'live-event-stream widget via the contract-declared omninode_internal '
  'write path.';

SELECT 1 / count(*) AS omninode_runtime_live_events_insert_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'live_events'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'INSERT';
