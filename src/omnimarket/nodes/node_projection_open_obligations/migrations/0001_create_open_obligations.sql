-- =============================================================================
-- MIGRATION: omninode_internal.open_obligations -- "what is currently owed"
-- =============================================================================
-- Ticket: OMN-17019 (C9 -- obligation-kind work events + the materialized
--         open-obligations projection). Parent epic: OMN-16176 (L0 -> L1 -> L2
--         ledger ladder). Sibling surface: omninode_internal.work_events
--         (OMN-16180), whose migration this file follows structurally.
-- Version: 1.0.0
--
-- WHY THIS FILE EXISTS
--   Today "what is owed" lives in prose: a session-goal markdown file, a
--   rolling plan, an open-ask section of a hand-appended ledger. A document
--   cannot be queried, cannot be arbitrated, and cannot prove it is complete --
--   which is how asks got dropped without anything noticing. This table is the
--   materialized fold of the five work-obligation events (created,
--   transferred, satisfied, superseded, abandoned) declared in the emit
--   registry under the existing onex.evt.omniclaude.* namespace. The
--   append-only event stream stays the authority; this is a VIEW of it, and
--   every markdown surface becomes a renderer over this table rather than a
--   store.
--
-- WHY ONE ROW PER OBLIGATION AND NOT ONE ROW PER EVENT
--   work_events (OMN-16180) already materializes one row per event. Asking
--   "what is currently owed" from that surface means folding the lifecycle at
--   read time, in every reader, identically -- which is exactly the kind of
--   convention-based agreement that drifts. The fold happens once, here, and
--   readers filter on `state`.
--
-- WHY THE FOLD IS SAFE WITHOUT ANY CROSS-DISPATCH REDUCER STATE
--   Two mechanisms, together:
--
--   1. COLUMN OWNERSHIP. Each event kind writes ONLY the columns it owns.
--      `created` owns created_at / asked_by / original_owed_by /
--      acceptance_condition / opened_summary / ticket_id. `transferred` owns
--      transferred_owed_by. The three terminal kinds own closed_state /
--      closed_at and their own evidence columns. Every adapter's UPSERT is a
--      targeted-column merge (`ON CONFLICT ... DO UPDATE SET` naming only the
--      incoming columns -- postgres_sync_database.py, sqlite_database.py and
--      InmemoryDatabaseAdapter agree byte-for-byte, per OMN-15598), so columns
--      an event does not name survive untouched.
--
--   2. DERIVED STATE. `state` and `owed_by` are GENERATED ALWAYS ... STORED
--      columns over the owned columns. Nothing can write them -- not the
--      handler, not a replay, not a hand-run UPDATE.
--
--   Together these make the fold correct under the case that actually happens:
--   a consumer restarting from an EARLIER partition offset re-delivers
--   `created` after `satisfied` was already applied. Because `created` never
--   touches closed_state, the re-delivery cannot reopen a closed obligation.
--   A design that stored a writable `state` column would silently reopen it,
--   and the projection would then report an obligation as owed that was
--   delivered days ago.
--
-- WHY THERE IS NO EXPIRY, TTL, OR DELETE ANYWHERE IN THIS FILE
--   Nothing in this schema removes a row or ages one out. An obligation leaves
--   the open set exactly one way: a recorded terminal event that names its own
--   evidence (a delivered artifact, a named successor, or a stated reason). A
--   time-based sweep that dropped stale rows would recreate the silent-drop
--   failure this surface exists to end, and the runtime role below is granted
--   SELECT/INSERT/UPDATE and deliberately NOT DELETE.
--
-- WHY omninode_internal AND NOT public
--   A net-new relation has no reason to land in the legacy default schema. The
--   OMN-15361 application-database SQL gate
--   (omnibase_infra/scripts/ci/check_application_database_sql.py) requires
--   deployable SQL to name a schema explicitly, and its
--   `_LEGACY_DEFAULT_SCHEMA_SQL_EXACT_PATHS` exemption list exists for
--   PRE-EXISTING relations the governed OMN-15359 cutover has not moved yet --
--   not for new ones. Creating directly in omninode_internal means this file
--   needs no exemption entry, now or at cutover. Same choice, same reasons, as
--   node_projection_work_events/migrations/0001_create_work_events.sql
--   (OMN-16180) and node_projection_live_events/migrations/
--   0002_create_omninode_internal_live_events.sql (OMN-15819).
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
--   no-op. No DROP, no TRUNCATE, no unguarded ALTER. This file is additive and
--   has never been applied anywhere; it does not edit any migration that has.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Precondition: the schema must exist. pg_namespace needs no schema-level
--    privilege to read, so this probe is safe under any connecting role, and it
--    runs FIRST because has_schema_privilege() below RAISES (rather than
--    returning false) for a schema name that does not exist.
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS omninode_internal_schema_exists_precondition
  FROM pg_catalog.pg_namespace
 WHERE nspname = 'omninode_internal';

-- -----------------------------------------------------------------------------
-- 2. Precondition: CURRENT_USER must hold USAGE, CREATE, and
--    USAGE WITH GRANT OPTION on omninode_internal. Checked against
--    current_user, not a hardcoded role, because the connecting identity
--    differs by lane. USAGE WITH GRANT OPTION is a SEPARATE requirement from
--    plain USAGE: section 7 re-grants schema USAGE onward to omninode_runtime,
--    and Postgres silently no-ops that onward grant (`WARNING: no privileges
--    were granted`) unless the granting role holds grant option for that
--    specific privilege -- which would leave the runtime write path broken
--    while this migration reported success.
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
--    explains is rejected. OMN-16993 provisions it. The role is asserted, not
--    created.
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS omninode_runtime_role_exists_precondition
  FROM pg_catalog.pg_roles
 WHERE rolname = 'omninode_runtime';

-- -----------------------------------------------------------------------------
-- 4. The open-obligations surface.
--
--    COLUMN SEMANTICS, grouped by WHICH EVENT KIND OWNS THE COLUMN. That
--    grouping is the schema's core invariant, not documentation convenience:
--    the fold is only replay-safe because no two kinds write the same column
--    (see the header, "WHY THE FOLD IS SAFE").
--
--    -- identity ------------------------------------------------------------
--    obligation_id        Stable identity across the whole lifecycle, and the
--                         event partition key. Every event about one obligation
--                         lands in one partition, so the fold resolves by
--                         partition offset rather than by wall clock.
--
--    -- written by EVERY kind ------------------------------------------------
--    last_event_kind      The most recently APPLIED event kind. Display and
--                         debugging only.
--    last_event_at        emitted_at of the most recently APPLIED event. NOT
--                         "the newest event": a consumer restarting from an
--                         earlier offset rewinds this and then rolls it forward
--                         again as the partition is re-consumed. Nothing is
--                         derived from it, precisely because it can rewind.
--    actor_kind/actor_id  Who recorded that event (ModelActor discriminator +
--                         identity). 'session' for a CLI/session actor, 'node'
--                         for a node actor; both are representable with no
--                         migration.
--    source_topic         Provenance: which bus topic produced the write.
--    payload              The projected event body, for fields not promoted to
--                         a column.
--    ingested_at          Projection-side write time. NOT the event time -- the
--                         gap between this and last_event_at is projection lag.
--
--    -- written ONLY by work.obligation.created ------------------------------
--    created_at           emitted_at of the opening event.
--    asked_by             Who asked for it.
--    original_owed_by     Who owed it at creation.
--    acceptance_condition What would make this satisfied. Required at creation
--                         with no default: an obligation with no acceptance
--                         condition can never be proven satisfied, which is the
--                         "declared done, never delivered" failure mode C9 was
--                         filed against.
--    opened_summary       The bounded narrative of the ask itself, kept
--                         separate from later events' summaries so the original
--                         request text is never overwritten by a status note.
--    ticket_id            Nullable. An obligation may exist with no ticket, and
--                         a ticket is NOT what closes one (see evidence_uri).
--
--    -- written ONLY by work.obligation.transferred --------------------------
--    transferred_owed_by  The current owner after the most recent transfer.
--                         Separate from original_owed_by so a re-delivered
--                         `created` cannot hand the obligation back to a
--                         previous owner.
--
--    -- written ONLY by the three terminal kinds -----------------------------
--    closed_state         'satisfied' | 'superseded' | 'abandoned'. The ONLY
--                         column the derived `state` reads. NULL means open.
--    closed_at            emitted_at of the terminal event.
--    evidence_uri         satisfied: the delivered artifact reference.
--    delivery_state       satisfied: how it was delivered. Both are required
--                         together, because off-rails rev 2 (A14) closes an
--                         obligation on a delivered artifact plus a delivery
--                         state and explicitly NOT on a ticket id -- a ticket
--                         moving to Done is not evidence anything reached
--                         anyone.
--    superseded_by        superseded: the successor obligation_id. A structured
--                         reference, never a free-text note, so the successor
--                         chain is walkable.
--    abandon_reason       abandoned: why it was dropped. Recorded so that a
--                         drop is a decision with an author, not a silence.
--
--    -- DERIVED, never written ------------------------------------------------
--    state                COALESCE(closed_state, 'open'). "What is currently
--                         owed" is `WHERE state = 'open'`. Generated rather
--                         than stored-and-maintained so it cannot be set to
--                         disagree with the recorded facts.
--    owed_by              COALESCE(transferred_owed_by, original_owed_by). The
--                         current debtor, derived for the same reason.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS omninode_internal.open_obligations (
  obligation_id         TEXT        PRIMARY KEY,
  last_event_kind       TEXT        NOT NULL DEFAULT '',
  last_event_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  actor_kind            TEXT        NOT NULL DEFAULT 'session',
  actor_id              TEXT        NOT NULL DEFAULT '',
  source_topic          TEXT        NOT NULL DEFAULT '',
  payload               JSONB       NOT NULL DEFAULT '{}',
  ingested_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at            TIMESTAMPTZ,
  asked_by              TEXT,
  original_owed_by      TEXT,
  acceptance_condition  TEXT,
  opened_summary        TEXT,
  ticket_id             TEXT,
  transferred_owed_by   TEXT,
  closed_state          TEXT,
  closed_at             TIMESTAMPTZ,
  evidence_uri          TEXT,
  delivery_state        TEXT,
  superseded_by         TEXT,
  abandon_reason        TEXT,
  state                 TEXT GENERATED ALWAYS AS (COALESCE(closed_state, 'open')) STORED,
  owed_by               TEXT GENERATED ALWAYS AS (COALESCE(transferred_owed_by, original_owed_by)) STORED
);

-- ---- BEGIN OMN-15376 shape reconciliation: omninode_internal.open_obligations ----
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
-- a pre-existing non-canonical table. Section 6 therefore asserts the
-- constraints are actually present afterward, and asserts `state` and `owed_by`
-- are GENERATED, so a shape-degraded table with a writable state column fails
-- loudly instead of silently accepting a hand-set lifecycle state.
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS obligation_id TEXT;
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS last_event_kind TEXT DEFAULT '';
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS last_event_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS actor_kind TEXT DEFAULT 'session';
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS actor_id TEXT DEFAULT '';
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS source_topic TEXT DEFAULT '';
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS payload JSONB DEFAULT '{}';
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS asked_by TEXT;
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS original_owed_by TEXT;
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS acceptance_condition TEXT;
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS opened_summary TEXT;
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS ticket_id TEXT;
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS transferred_owed_by TEXT;
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS closed_state TEXT;
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS evidence_uri TEXT;
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS delivery_state TEXT;
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS superseded_by TEXT;
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS abandon_reason TEXT;
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS state TEXT
  GENERATED ALWAYS AS (COALESCE(closed_state, 'open')) STORED;
ALTER TABLE omninode_internal.open_obligations
  ADD COLUMN IF NOT EXISTS owed_by TEXT
  GENERATED ALWAYS AS (COALESCE(transferred_owed_by, original_owed_by)) STORED;
-- ---- END OMN-15376 shape reconciliation: omninode_internal.open_obligations ----

-- -----------------------------------------------------------------------------
-- 5. Indexes. The access paths are "everything currently owed" (the whole point
--    of the surface), "what does this actor owe", "what is open against this
--    ticket", and newest-first for rendering.
--
--    The first three are PARTIAL on state = 'open'. The open set is the hot
--    read and stays small while the closed history grows without bound, so a
--    partial index keeps the common query proportional to what is owed rather
--    than to everything ever owed.
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_open_obligations_state
  ON omninode_internal.open_obligations (state, last_event_at DESC);

CREATE INDEX IF NOT EXISTS idx_open_obligations_open_owed_by
  ON omninode_internal.open_obligations (owed_by, last_event_at DESC)
  WHERE state = 'open';

CREATE INDEX IF NOT EXISTS idx_open_obligations_open_ticket
  ON omninode_internal.open_obligations (ticket_id, last_event_at DESC)
  WHERE state = 'open' AND ticket_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_open_obligations_last_event_at
  ON omninode_internal.open_obligations (last_event_at DESC);

-- -----------------------------------------------------------------------------
-- 6. Shape post-conditions -- BEFORE any GRANT below, deliberately.
--    run-forward-migrations.sh invokes `psql -v ON_ERROR_STOP=1 -f` with no
--    --single-transaction, so every statement autocommits independently. If
--    these ran AFTER the grant, a failed assertion would still leave the
--    runtime write grant committed on a table this migration just rejected.
--    Grant last means ON_ERROR_STOP aborts before any GRANT is reached.
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS open_obligations_exists_assertion
  FROM information_schema.tables
 WHERE table_schema = 'omninode_internal' AND table_name = 'open_obligations';

-- The PRIMARY KEY must be exactly (obligation_id). A pre-existing table with a
-- primary key on a different column would pass a looser "some PK exists" check
-- while admitting two rows for one obligation -- which is the whole fold.
SELECT 1 / count(*) AS open_obligations_primary_key_assertion
  FROM (
    SELECT tc.constraint_name
      FROM information_schema.table_constraints tc
      JOIN information_schema.key_column_usage kcu
        ON kcu.constraint_name = tc.constraint_name
       AND kcu.table_schema = tc.table_schema
     WHERE tc.table_schema = 'omninode_internal'
       AND tc.table_name = 'open_obligations'
       AND tc.constraint_type = 'PRIMARY KEY'
     GROUP BY tc.constraint_name
    HAVING count(*) = 1 AND bool_and(kcu.column_name = 'obligation_id')
  ) AS assertion;

SELECT 1 / count(*) AS open_obligations_not_null_columns_assertion
  FROM (
    SELECT 1
     WHERE (
       SELECT count(*)
         FROM information_schema.columns
        WHERE table_schema = 'omninode_internal'
          AND table_name = 'open_obligations'
          AND column_name IN (
                'obligation_id', 'last_event_kind', 'last_event_at',
                'actor_kind', 'actor_id', 'source_topic', 'payload',
                'ingested_at'
              )
          AND is_nullable = 'NO'
     ) = 8
  ) AS assertion;

-- The load-bearing one. `state` and `owed_by` MUST be generated, or the
-- replay-safety argument in this file's header is false: a writable state
-- column can be set to disagree with the closed_state fact it is supposed to
-- summarise, by a replay or by a hand-run UPDATE.
SELECT 1 / count(*) AS open_obligations_derived_columns_are_generated_assertion
  FROM (
    SELECT 1
     WHERE (
       SELECT count(*)
         FROM information_schema.columns
        WHERE table_schema = 'omninode_internal'
          AND table_name = 'open_obligations'
          AND column_name IN ('state', 'owed_by')
          AND is_generated = 'ALWAYS'
     ) = 2
  ) AS assertion;

SELECT 1 / count(*) AS open_obligations_payload_is_jsonb_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'open_obligations'
   AND column_name = 'payload'
   AND data_type = 'jsonb';

-- -----------------------------------------------------------------------------
-- 7. Runtime-owns-DB doctrine (feedback_only_runtime_touches_database): the
--    projection writes under the omninode_runtime binding, so that role gets
--    exactly the projection writer's scope -- SELECT, INSERT, UPDATE and NO
--    DELETE. A projection upserts; it does not prune its own table. Here the
--    absence of DELETE is also the schema-level expression of the no-expiry
--    rule in this file's header: an obligation leaves the open set only via a
--    recorded terminal event, never by being swept away.
--    Issuing the grant in the CREATING migration is what stops this surface
--    from repeating the OMN-16993 defect, where a projection table existed for
--    weeks with no runtime grant and every write failed authentication.
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;
GRANT SELECT, INSERT, UPDATE ON omninode_internal.open_obligations TO omninode_runtime;

COMMENT ON TABLE omninode_internal.open_obligations IS
  'Open-obligations projection (OMN-17019): the materialized fold of the five '
  'work.obligation.* events, one row per obligation. "What is currently owed" '
  'is WHERE state = ''open''. The append-only event log is the authority; every '
  'markdown surface that shows obligations is a renderer over this table.';

SELECT 1 / count(*) AS omninode_runtime_open_obligations_insert_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'open_obligations'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS omninode_runtime_open_obligations_select_grant_assertion
  FROM information_schema.role_table_grants
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'open_obligations'
   AND grantee = 'omninode_runtime'
   AND privilege_type = 'SELECT';
