-- =============================================================================
-- MIGRATION: ranked runtime-error fingerprints (the Lab Errors read model)
-- =============================================================================
-- Ticket:  OMN-18770 (C3 of epic OMN-18767 — lab observability tab)
-- Owner:   omnimarket.nodes.node_projection_runtime_error_fingerprints
-- Version: 1.0.0
--
-- WHY THIS EXISTS
--   The only error signal a lab panel could read was consumer-flow.v1:
--   handler_errors, messages_dlq, and the STALLED / STARVED flow states. That
--   answers "is something failing" and cannot answer "what is failing".
--
--   The surface that could — omnibase_infra.public.runtime_error_triage — held
--   69 rows on 2026-09-18, every one incident_state 'open', nothing written
--   since 2026-09-10, and 100% of them error_category 'unknown'. It is also in
--   another service's database, in `public`, and owned by an omnibase_infra
--   effect node. This is the omnimarket projection read model, like every
--   other node_projection_* table, and it is written by the reducer that
--   derives the category rather than by the effect that inherited it.
--
-- WHY THE FINGERPRINT IS THE PRIMARY KEY
--   The ranking IS the product. A flood of identical traces has to collapse
--   into ONE row whose occurrence_count rises — that collapse is what makes a
--   truncated page useful, and it is the one genuinely portable idea in the
--   archived runtime-errors view. A surrogate key with one row per occurrence
--   would reproduce the raw log the archive already had 372 MB of.
--
-- WHY occurrence_count IS NOT NULLABLE AND HAS NO DEFAULT NULL
--   Unlike consumer_flow_windows, there is no "observed but unknown" state
--   here: a row exists because an occurrence was observed, so the count is
--   always at least that occurrence. An absent error class has no row at all,
--   which is a different and correctly-representable fact.
--
-- WHY correlation_id IS A COLUMN AND NOT A JOIN
--   It is the operator's stated debugging affordance: click an error row, get
--   its trace. A fingerprint spans many traces, so the column holds the MOST
--   RECENT occurrence's id — the one still inside the trace surface's
--   retention window and therefore the one that will actually resolve.
--
-- WHY category_evidence IS STORED
--   A category with no recorded basis is indistinguishable from a guess. This
--   whole table exists because 69 unexplained 'unknown's were, and an operator
--   ranking by occurrence count needs to know whether a category came from an
--   exception class or from a keyword before acting on it.
--
-- WHY omninode_internal, EXPLICITLY QUALIFIED
--   omnibase_infra's scripts/ci/check_application_database_sql.py (OMN-15361)
--   rejects an UNQUALIFIED application relation target — a bare CREATE
--   resolves against whatever search_path the runner carries — and prohibits
--   `public` outright for application relations. No CREATE SCHEMA here: the
--   node-owned migration loop connects to the application database where
--   omninode_internal already exists, and a CREATE SCHEMA is exactly what
--   failed with "permission denied for database omnibase_infra" in OMN-16759.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.runtime_error_fingerprints (
    -- sha256(logger_family:derived_category:message_template)[:16], derived
    -- over the category this projection DERIVED, never the one the producer
    -- stamped. The producer hashed its own (wrong) category into its digest,
    -- so its fingerprints and this ranking would disagree about identity.
    fingerprint        TEXT        NOT NULL,

    logger_name        TEXT        NOT NULL,

    -- kafka_consumer | kafka_producer | database | http_client | http_server
    -- | runtime | unknown. Derived in the projection, never carried on the
    -- producing event (envelope purity).
    error_category     TEXT        NOT NULL,

    -- exception_type | logger_prefix | message_keyword | none — WHICH rule
    -- produced error_category.
    category_evidence  TEXT        NOT NULL,

    -- critical | error | warning
    severity           TEXT        NOT NULL,

    message_template   TEXT        NOT NULL,
    exception_type     TEXT        NOT NULL DEFAULT '',

    -- The ranking column. Accumulated across occurrences, including the
    -- occurrences the producer's rate limiter already collapsed into one
    -- event (occurrence_count_local), so a suppressed flood still ranks.
    occurrence_count   BIGINT      NOT NULL,

    -- Most recent occurrence's correlation id. Empty string, never NULL, so a
    -- reader never has to distinguish "no id" from "column absent".
    correlation_id     TEXT        NOT NULL DEFAULT '',

    service_name       TEXT        NOT NULL DEFAULT '',
    hostname           TEXT        NOT NULL DEFAULT '',

    -- Event time on both, not a wall clock: the row is a statement about the
    -- events, so a replay reproduces it rather than re-dating it.
    first_seen_at      TIMESTAMPTZ NOT NULL,
    last_seen_at       TIMESTAMPTZ NOT NULL,

    -- Database-assigned monotonic page boundary for the projection API. Added
    -- in the create migration rather than a follow-up because this table has
    -- never existed; the consumer-flow pair needed 0001 only because 0000 was
    -- already merged and immutable.
    projection_cursor  BIGSERIAL   NOT NULL,

    PRIMARY KEY (fingerprint)
);

-- SHAPE reconciliation, not merely existence (OMN-15376 class).
--
--   CREATE TABLE IF NOT EXISTS no-ops against a pre-existing table of the
--   same name, whatever shape it has. On a database where an earlier or
--   drifted `runtime_error_fingerprints` already exists, every column
--   declared above would silently not arrive, and the FIRST column-dependent
--   statement after it -- the ranking index immediately below -- fails and
--   takes the whole forward-migration run with it. One guarded ADD COLUMN per
--   declared column makes the create idempotent in SHAPE and not merely in
--   existence.
--
--   NOT NULL is deliberately absent from the ADDs that carry no DEFAULT: a
--   NOT NULL column added to a table that already has rows is refused by
--   Postgres. On a virgin database the CREATE above already applied the
--   constraint; on a drifted one an added column is nullable and the row is
--   visibly incomplete, which is the honest outcome. The primary key likewise
--   belongs to the CREATE and is not re-asserted here.
ALTER TABLE omninode_internal.runtime_error_fingerprints
    ADD COLUMN IF NOT EXISTS fingerprint       TEXT;
ALTER TABLE omninode_internal.runtime_error_fingerprints
    ADD COLUMN IF NOT EXISTS logger_name       TEXT;
ALTER TABLE omninode_internal.runtime_error_fingerprints
    ADD COLUMN IF NOT EXISTS error_category    TEXT;
ALTER TABLE omninode_internal.runtime_error_fingerprints
    ADD COLUMN IF NOT EXISTS category_evidence TEXT;
ALTER TABLE omninode_internal.runtime_error_fingerprints
    ADD COLUMN IF NOT EXISTS severity          TEXT;
ALTER TABLE omninode_internal.runtime_error_fingerprints
    ADD COLUMN IF NOT EXISTS message_template  TEXT;
ALTER TABLE omninode_internal.runtime_error_fingerprints
    ADD COLUMN IF NOT EXISTS exception_type    TEXT DEFAULT '';
ALTER TABLE omninode_internal.runtime_error_fingerprints
    ADD COLUMN IF NOT EXISTS occurrence_count  BIGINT;
ALTER TABLE omninode_internal.runtime_error_fingerprints
    ADD COLUMN IF NOT EXISTS correlation_id    TEXT DEFAULT '';
ALTER TABLE omninode_internal.runtime_error_fingerprints
    ADD COLUMN IF NOT EXISTS service_name      TEXT DEFAULT '';
ALTER TABLE omninode_internal.runtime_error_fingerprints
    ADD COLUMN IF NOT EXISTS hostname          TEXT DEFAULT '';
ALTER TABLE omninode_internal.runtime_error_fingerprints
    ADD COLUMN IF NOT EXISTS first_seen_at     TIMESTAMPTZ;
ALTER TABLE omninode_internal.runtime_error_fingerprints
    ADD COLUMN IF NOT EXISTS last_seen_at      TIMESTAMPTZ;
ALTER TABLE omninode_internal.runtime_error_fingerprints
    ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;

-- The exposure's own ordering, so the ranked page is an index scan rather
-- than a sort of the whole table.
CREATE INDEX IF NOT EXISTS idx_runtime_error_fingerprints_rank
    ON omninode_internal.runtime_error_fingerprints
    (occurrence_count DESC, last_seen_at DESC);

-- "What is failing in this subsystem right now" — the Lab Errors widget's
-- second query, once an operator has picked a category off the ranked page.
CREATE INDEX IF NOT EXISTS idx_runtime_error_fingerprints_category_time
    ON omninode_internal.runtime_error_fingerprints
    (error_category, last_seen_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_error_fingerprints_cursor
    ON omninode_internal.runtime_error_fingerprints (projection_cursor);
