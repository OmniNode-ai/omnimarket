-- =============================================================================
-- MIGRATION: omninode_internal.work_events -- the L1 work-ledger surface
-- =============================================================================
-- Ticket: OMN-16180 (C4 -- one projection node contract materializes the
--         queryable work-event surface). Parent epic: OMN-16176 (L0 -> L1 -> L2
--         ledger ladder). Schema kinds: OMN-16177 (C1, omnibase_core work
--         event models, merged omnibase_core#1563).
-- Version: 1.0.0
--
-- WHY THIS FILE EXISTS
--   The rolling work ledger is a hand-written markdown file
--   (omni_home/docs/tracking/ROLLING_WORK_LEDGER.md, appended under a lock by
--   scripts/ledger_lock.py). It is the L0 record and stays authoritative.
--   L1 is this table: the same narrative, materialized from events that are
--   ALREADY flowing on the bus, so the ledger can be GENERATED rather than
--   hand-typed. OMN-16176's own ladder requires L0 and L1 to dual-write during
--   the transition; nothing here retires ledger_lock.py (that is OMN-16183/C7).
--
--   The four omniclaude hook topics this node subscribes carry real session
--   traffic today -- live-verified 2026-08-29 on the stability-lane broker
--   (compose service omnibase-infra-stability-test-redpanda, the lane of record
--   for hook events; the address is deliberately not written here -- it belongs
--   in the contract overlay, not in a migration comment), and materialized by
--   the sibling node_projection_session_replay into
--   public.session_replay_snapshots once OMN-16993 issued that table's
--   omninode_runtime grant. This migration creates the second, work-shaped
--   projection of that same live stream.
--
-- WHY omninode_internal AND NOT public
--   A net-new relation has no reason to land in the legacy default schema. The
--   OMN-15361 application-database SQL gate
--   (omnibase_infra/scripts/ci/check_application_database_sql.py) requires
--   deployable SQL to name a schema explicitly, and its
--   `_LEGACY_DEFAULT_SCHEMA_SQL_EXACT_PATHS` exemption list exists for
--   PRE-EXISTING relations that the governed OMN-15359 cutover has not moved
--   yet -- not for new ones. Creating directly in omninode_internal means this
--   file needs no exemption entry, now or at cutover.
--
--   Canonical precedent followed verbatim here:
--   node_projection_live_events/migrations/0002_create_omninode_internal_live_events.sql
--   (OMN-15819) -- same schema, same precondition idiom, same grant scope.
--
-- WHY EVERY PRECONDITION IS A BARE SELECT AND NOT A DO/RAISE BLOCK
--   The application-database SQL gate's `_requires_dynamic_sql_rejection`
--   rejects ANY new `DO $$ ... $$` block in a migration touching an
--   application-topology schema: procedural execution cannot be proven to
--   target only statically-known relations. So every assertion below uses the
--   statically-provable `SELECT 1 / count(*)` division-by-zero idiom --
--   Postgres's own fail-closed primitive when the guarded condition is false.
--   The tradeoff is a generic `division by zero` error rather than a
--   hand-authored message; the remedy for each is documented inline.
--
-- IDEMPOTENCY
--   CREATE TABLE / CREATE INDEX are IF NOT EXISTS. Every precondition and
--   post-condition is a read-only catalog probe. Re-running this file is a
--   no-op. No DROP, no TRUNCATE, no unguarded ALTER.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Precondition: the schema must exist. pg_namespace needs no schema-level
--    privilege to read, so this probe is safe under any connecting role, and
--    it runs FIRST because has_schema_privilege() below RAISES (rather than
--    returning false) for a schema name that does not exist.
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS omninode_internal_schema_exists_precondition
  FROM pg_catalog.pg_namespace
 WHERE nspname = 'omninode_internal';

-- -----------------------------------------------------------------------------
-- 2. Precondition: CURRENT_USER must hold USAGE, CREATE, and
--    USAGE WITH GRANT OPTION on omninode_internal. Checked against
--    current_user, not a hardcoded role, because the connecting identity
--    differs by lane (role_omnidash on the k8s/RDS lane, the postgres
--    superuser on the compose lanes). USAGE WITH GRANT OPTION is a SEPARATE
--    requirement from plain USAGE: section 7 re-grants schema USAGE onward to
--    omninode_runtime, and Postgres silently no-ops that onward grant
--    (`WARNING: no privileges were granted`) unless the granting role holds
--    grant option for that specific privilege -- which would leave the runtime
--    write path broken while this migration reported success. Proven live
--    while authoring OMN-15819's sibling file.
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
-- 3. Precondition: omninode_runtime must already exist. Provisioning it is not
--    this migration's concern -- CREATE ROLE has no IF NOT EXISTS form, so
--    idempotent creation would require exactly the DO block section 2's header
--    explains is rejected. The role is asserted, not created.
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS omninode_runtime_role_exists_precondition
  FROM pg_catalog.pg_roles
 WHERE rolname = 'omninode_runtime';

-- -----------------------------------------------------------------------------
-- 4. The work-event surface.
--
--    COLUMN SEMANTICS (each maps to an OMN-16177 ModelWorkEventBase field
--    where one exists; the projection-only columns are named as such):
--
--    event_id     Content-addressed idempotency key: a SHA-256 over
--                 (source_topic, actor_id, emitted_at, discriminator). This is
--                 deliberately NOT a per-session sequence counter. The sibling
--                 node_projection_session_replay derives its key from
--                 (session_id, sequence) while threading NO reducer state
--                 across dispatches, so `sequence` is always 0 and its
--                 UNIQUE (session_id, sequence) collapses every event of a
--                 session onto ONE row -- live-observed 2026-08-29: 14 rows
--                 total, one per session, for a topic carrying thousands of
--                 records. A content-addressed key cannot collapse that way and
--                 makes double-replay byte-identical (OMN-16180 acceptance 2)
--                 without any cross-dispatch state.
--    emitted_at   Emitter-assigned event time off the wire. DISPLAY SORT ONLY
--                 -- it is not a claimed global ordering (OMN-16177's ordering
--                 contract, deterministic-truth doctrine section 4). Per-actor
--                 total order comes from the partition offset, not this column.
--    event_kind   The work-event kind this record projects to.
--    actor_kind   ModelActor discriminator: 'session' | 'node'. Present so the
--                 C1 actor union round-trips through this surface; every row
--                 this node writes today is 'session' (the omniclaude hooks
--                 are session actors), and a node actor is representable
--                 without a migration.
--    actor_id     The actor's identity: session_handle for a session actor,
--                 node_id for a node actor.
--    ticket_id    Nullable -- hook events carry no ticket reference. Populated
--                 by the C2 emit path when work events start carrying one.
--    summary      Bounded narrative, <= 2000 chars, enforced by CHECK below.
--    source_topic Provenance: which bus topic produced this row.
--    payload      The projected event body, for fields this table does not
--                 promote to a column.
--    ingested_at  Projection-side write time. NOT the event time -- the gap
--                 between this and emitted_at is the projection lag.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS omninode_internal.work_events (
  event_id      TEXT        PRIMARY KEY,
  emitted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  event_kind    TEXT        NOT NULL DEFAULT '',
  actor_kind    TEXT        NOT NULL DEFAULT 'session',
  actor_id      TEXT        NOT NULL DEFAULT '',
  ticket_id     TEXT,
  summary       TEXT        NOT NULL DEFAULT '',
  source_topic  TEXT        NOT NULL DEFAULT '',
  payload       JSONB       NOT NULL DEFAULT '{}',
  ingested_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: omninode_internal.work_events ----
-- CREATE TABLE IF NOT EXISTS above silently no-ops if a table of this name
-- already exists with a different shape, and the CREATE INDEX statements below
-- guard the index NAME rather than the COLUMN -- so a drifted pre-existing
-- table would fail the whole migration Job at the first index, one deploy cycle
-- at a time (OMN-15376, OMN-15302). Required by the static gate
-- omnibase_infra/tests/ci/test_node_migration_shape_reconciliation.py: every
-- column declared above must also be covered by a guarded ADD COLUMN here.
-- Every one is a no-op on the fresh-create path this file actually exercises.
--
-- WHAT THIS BLOCK CANNOT DO: ADD COLUMN IF NOT EXISTS only ever adds a NULLABLE
-- column -- it cannot retroactively apply the PRIMARY KEY or any NOT NULL onto
-- a pre-existing non-canonical table, and a DO block that tried is exactly the
-- procedural shape the application-database gate rejects. Section 6 therefore
-- asserts the constraints are actually present afterward, so a shape-degraded
-- table fails loudly instead of silently accepting incomplete rows.
ALTER TABLE omninode_internal.work_events
  ADD COLUMN IF NOT EXISTS event_id TEXT;
ALTER TABLE omninode_internal.work_events
  ADD COLUMN IF NOT EXISTS emitted_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.work_events
  ADD COLUMN IF NOT EXISTS event_kind TEXT DEFAULT '';
ALTER TABLE omninode_internal.work_events
  ADD COLUMN IF NOT EXISTS actor_kind TEXT DEFAULT 'session';
ALTER TABLE omninode_internal.work_events
  ADD COLUMN IF NOT EXISTS actor_id TEXT DEFAULT '';
ALTER TABLE omninode_internal.work_events
  ADD COLUMN IF NOT EXISTS ticket_id TEXT;
ALTER TABLE omninode_internal.work_events
  ADD COLUMN IF NOT EXISTS summary TEXT DEFAULT '';
ALTER TABLE omninode_internal.work_events
  ADD COLUMN IF NOT EXISTS source_topic TEXT DEFAULT '';
ALTER TABLE omninode_internal.work_events
  ADD COLUMN IF NOT EXISTS payload JSONB DEFAULT '{}';
ALTER TABLE omninode_internal.work_events
  ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ DEFAULT NOW();
-- ---- END OMN-15376 shape reconciliation: omninode_internal.work_events ----

-- -----------------------------------------------------------------------------
-- 5. Indexes. The ledger view's own query is "newest first, optionally filtered
--    by actor or ticket", so those are the three access paths indexed here.
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_work_events_emitted_at
  ON omninode_internal.work_events (emitted_at DESC);

CREATE INDEX IF NOT EXISTS idx_work_events_actor
  ON omninode_internal.work_events (actor_id, emitted_at DESC);

CREATE INDEX IF NOT EXISTS idx_work_events_kind
  ON omninode_internal.work_events (event_kind, emitted_at DESC);

CREATE INDEX IF NOT EXISTS idx_work_events_ticket
  ON omninode_internal.work_events (ticket_id, emitted_at DESC)
  WHERE ticket_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 6. Shape post-conditions -- BEFORE any GRANT below, deliberately.
--    run-forward-migrations.sh invokes `psql -v ON_ERROR_STOP=1 -f` with no
--    --single-transaction, so every statement autocommits independently. If
--    these ran AFTER the grant, a failed assertion would still leave the
--    runtime write grant committed on a table this migration just rejected.
--    Grant last means ON_ERROR_STOP aborts before any GRANT is reached.
--
--    Each check asserts the EXACT column set rather than "some constraint of
--    this type exists" -- a pre-existing table with a PRIMARY KEY on the wrong
--    column would silently pass a looser check while still admitting duplicate
--    event_id values, defeating the idempotency this surface is built on.
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS work_events_exists_assertion
  FROM information_schema.tables
 WHERE table_schema = 'omninode_internal' AND table_name = 'work_events';

SELECT 1 / count(*) AS work_events_primary_key_assertion
  FROM (
    SELECT tc.constraint_name
      FROM information_schema.table_constraints tc
      JOIN information_schema.key_column_usage kcu
        ON kcu.constraint_name = tc.constraint_name
       AND kcu.table_schema = tc.table_schema
     WHERE tc.table_schema = 'omninode_internal'
       AND tc.table_name = 'work_events'
       AND tc.constraint_type = 'PRIMARY KEY'
     GROUP BY tc.constraint_name
    HAVING count(*) = 1 AND bool_and(kcu.column_name = 'event_id')
  ) AS assertion;

SELECT 1 / count(*) AS work_events_not_null_columns_assertion
  FROM (
    SELECT 1
     WHERE (
       SELECT count(*)
         FROM information_schema.columns
        WHERE table_schema = 'omninode_internal'
          AND table_name = 'work_events'
          AND column_name IN (
                'event_id', 'emitted_at', 'event_kind', 'actor_kind',
                'actor_id', 'summary', 'source_topic', 'payload',
                'ingested_at'
              )
          AND is_nullable = 'NO'
     ) = 9
  ) AS assertion;

SELECT 1 / count(*) AS work_events_ticket_id_nullable_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'work_events'
   AND column_name = 'ticket_id'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS work_events_payload_is_jsonb_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'work_events'
   AND column_name = 'payload'
   AND data_type = 'jsonb';

-- -----------------------------------------------------------------------------
-- 7. Runtime-owns-DB doctrine (feedback_only_runtime_touches_database): the
--    projection writes under the omninode_runtime binding, so that role gets
--    exactly the projection writer's scope -- SELECT, INSERT, UPDATE and no
--    DELETE. A projection upserts; it does not reshape or prune its own table.
--    Identical scope to omninode_internal.live_events (OMN-15819) and to the
--    grant OMN-16993 had to issue after the fact for session_replay_snapshots.
--    Issuing it here, in the creating migration, is what stops this surface
--    from repeating that defect: the table cannot exist without the grant.
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;
GRANT SELECT, INSERT, UPDATE ON omninode_internal.work_events TO omninode_runtime;

COMMENT ON TABLE omninode_internal.work_events IS
  'L1 work-ledger surface (OMN-16180): work events projected from the live '
  'omniclaude session/hook topics by node_projection_work_events. L0 remains '
  'docs/tracking/ROLLING_WORK_LEDGER.md during the OMN-16176 transition.';

SELECT 1 / count(*) AS omninode_runtime_work_events_insert_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'work_events'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS omninode_runtime_work_events_select_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'work_events'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'SELECT';
