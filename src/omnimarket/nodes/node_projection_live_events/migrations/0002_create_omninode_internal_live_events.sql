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
--   onward `GRANT USAGE ... TO omninode_runtime` (step 6 below) a silent
--   no-op (`WARNING: no privileges were granted`), which would leave the
--   runtime write path broken while this migration reports success. See
--   precondition 2 below for the live citation.
--   This migration does NOT attempt that grant itself -- it has no rights to
--   issue it and mis-designing around that (e.g. self-escalating privilege)
--   is explicitly out of scope. Instead it ASSERTS the precondition
--   fail-fast, by design: a migration that silently no-ops or half-applies
--   against a schema it cannot use is a worse failure mode than a named,
--   loud refusal that points at the exact operator step still pending.
--
-- WHY has_schema_privilege AND NOT A TRY/CATCH ON THE CREATE TABLE
--   `to_regclass('omninode_internal.live_events')` itself raises
--   `permission denied for schema omninode_internal` (not a NULL result)
--   when the connecting role lacks USAGE -- confirmed live above. A bare
--   `CREATE TABLE IF NOT EXISTS omninode_internal.live_events (...)` without
--   USAGE fails the same way, ON_ERROR_STOP=1 kills the migration Job, and
--   the operator is left decoding a raw Postgres permission error instead
--   of being told which OMN-15819 step is outstanding. Asserting the
--   precondition first, with RAISE EXCEPTION naming the exact GRANT
--   statement needed, is strictly more useful and no more expensive.
--
-- IDEMPOTENCY
--   CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS / a
--   NOT-EXISTS-guarded GRANT-adjacent CREATE ROLE are all safe to re-run.
--   The two precondition DO blocks are read-only catalog probes -- re-run
--   is a no-op once the operator grant has landed. No ALTER of any kind
--   appears in this file.
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
--   omninode_runtime invariant). The omninode_runtime ROLE already exists on
--   the live target (live-confirmed via pg_roles, 2026-08-10) but is
--   guard-created here anyway, matching 099's own precedent exactly, so
--   this file is also correct on a fresh lane (e.g. the standalone
--   top-level-only "Migration Integration Test" CI scope) where it might
--   not.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Precondition: the schema must exist at all before any privilege check
--    on it is meaningful -- has_schema_privilege() RAISES (does not return
--    false) for a schema name that does not exist, which would surface here
--    as an unhelpful catalog error instead of a named OMN-15819 pointer.
--    pg_namespace needs no schema-level privilege to read, so this check is
--    safe under any connecting role.
-- -----------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = 'omninode_internal'
  ) THEN
    RAISE EXCEPTION
      'OMN-15819: schema omninode_internal does not exist in this database. '
      'It is created out-of-band on the managed/RDS lane (master-owned; see '
      'this file''s header) or by 098_create_omninode_internal_schema.sql on '
      'lanes where that flat file actually executes (e.g. compose, which has '
      'no cross-DB skip logic). This migration refuses to guess and will not '
      'create the schema itself.';
  END IF;
END;
$$;

-- -----------------------------------------------------------------------------
-- 2. Precondition: CURRENT_USER (whichever role is actually executing this
--    file) must hold USAGE + CREATE on omninode_internal. Checked against
--    current_user, not a hardcoded role name, because the connecting
--    identity differs by lane: role_omnidash on the k8s Job / managed RDS
--    lane (OMN-15313 NODE_DB_USER), but the `postgres` superuser on the
--    compose lane (POSTGRES_USER default, no per-role split there -- see
--    096's own docstring for the live readback proving this). A superuser
--    always reads back `true` here regardless of ACL rows, so this check is
--    a no-op on compose and a real, live-confirmed-false gate on RDS today.
--    Fails loud and names the exact operator remedy rather than letting the
--    CREATE TABLE below fail on a raw `permission denied for schema`.
-- -----------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT has_schema_privilege(current_user, 'omninode_internal', 'USAGE') THEN
    RAISE EXCEPTION
      'OMN-15819: % lacks USAGE on schema omninode_internal -- run OMN-15819 '
      'step 3 first (operator, one-time, as omninodeadmin master): GRANT '
      'USAGE, CREATE ON SCHEMA omninode_internal TO role_omnidash WITH '
      'GRANT OPTION; This migration refuses to guess at a schema it cannot '
      'use.', current_user;
  END IF;
  IF NOT has_schema_privilege(current_user, 'omninode_internal', 'CREATE') THEN
    RAISE EXCEPTION
      'OMN-15819: % lacks CREATE on schema omninode_internal -- run '
      'OMN-15819 step 3 first (operator, one-time, as omninodeadmin '
      'master): GRANT USAGE, CREATE ON SCHEMA omninode_internal TO '
      'role_omnidash WITH GRANT OPTION; This migration refuses to guess at '
      'a schema it cannot create in.', current_user;
  END IF;
  -- WITH GRANT OPTION, specifically for USAGE, is a SEPARATE precondition
  -- from plain USAGE above -- this migration re-grants schema USAGE onward
  -- to omninode_runtime (step 6 below), and Postgres requires the granting
  -- role to hold GRANT OPTION for that specific privilege, not merely the
  -- privilege itself, or the onward GRANT silently no-ops with
  -- `WARNING: no privileges were granted for "omninode_internal"` --
  -- proven live in an ephemeral sandbox while authoring this file: a plain
  -- (non-grant-option) USAGE grant to role_omnidash let this migration
  -- create the table and grant TABLE-level privileges (ownership of a
  -- self-created object carries its own grant rights), but the SCHEMA-level
  -- forward to omninode_runtime silently granted nothing, which would have
  -- left the runtime write path just as broken as before this migration
  -- (permission denied for schema, not UndefinedTable) while the migration
  -- itself reported success. This corrects the ticket's originally-stated
  -- operator recipe (OMN-15819 step 3), which did not specify
  -- WITH GRANT OPTION -- flagged in the PR body and on the ticket.
  IF NOT has_schema_privilege(
    current_user, 'omninode_internal', 'USAGE WITH GRANT OPTION'
  ) THEN
    RAISE EXCEPTION
      'OMN-15819: % has USAGE on schema omninode_internal but not WITH '
      'GRANT OPTION -- this migration forwards USAGE to omninode_runtime '
      '(step 6 below), which requires grant option, not just the '
      'privilege itself, or Postgres silently grants nothing. Re-run '
      'OMN-15819 step 3 as: GRANT USAGE, CREATE ON SCHEMA omninode_internal '
      'TO role_omnidash WITH GRANT OPTION;', current_user;
  END IF;
END;
$$;

-- -----------------------------------------------------------------------------
-- 3. omninode_runtime role existence, guarded exactly like 099's own
--    precedent (and 094/096 before it): CREATE only when absent, so a role
--    provisioned out-of-band on a managed instance is never re-created or
--    clobbered. NOLOGIN at create time -- LOGIN + password attach stays
--    deployment-owned and is never asserted here.
-- -----------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'omninode_runtime') THEN
    BEGIN
      CREATE ROLE omninode_runtime WITH
        NOLOGIN
        NOSUPERUSER
        NOBYPASSRLS
        NOCREATEDB
        NOCREATEROLE
        NOREPLICATION;
    EXCEPTION
      WHEN duplicate_object OR unique_violation THEN
        NULL; -- created concurrently by another migration path
    END;
  END IF;
END;
$$;

-- -----------------------------------------------------------------------------
-- 4. Canonical 10-column shape, copied verbatim from
--    docker/migrations/forward/099_create_omninode_internal_live_events.sql
--    -- see the file-header rationale above. Do not hand-retype; if the
--    shape ever needs to change, change 099 first and copy forward again.
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;

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
-- 6. Runtime-owns-DB doctrine: omninode_runtime read/write grant, identical
--    scope to 099's own (SELECT, INSERT, UPDATE -- no DELETE).
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;
GRANT SELECT, INSERT, UPDATE ON omninode_internal.live_events TO omninode_runtime;

COMMENT ON TABLE omninode_internal.live_events IS
  'Platform-wide bus event projection (internal-schema copy) -- delivered by '
  'the node-owned migration loop per OMN-15819; feeds the omnidash '
  'live-event-stream widget via the contract-declared omninode_internal '
  'write path.';

-- -----------------------------------------------------------------------------
-- 7. Post-conditions. Statically provable (no DO/RAISE), matching the
--    OMN-15361 application-database gate's requirement for deployable SQL
--    and 098/099's own convention.
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS omninode_internal_live_events_exists_assertion
  FROM information_schema.tables
 WHERE table_schema = 'omninode_internal' AND table_name = 'live_events';

SELECT 1 / count(*) AS omninode_runtime_live_events_insert_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'live_events'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'INSERT';
