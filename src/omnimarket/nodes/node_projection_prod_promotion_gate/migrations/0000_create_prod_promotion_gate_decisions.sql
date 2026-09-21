-- =============================================================================
-- MIGRATION: durable prod-promotion-gate decisions
-- =============================================================================
-- Ticket:  OMN-18999 (surface 2 of 4 under OMN-18946)
-- Owner:   omnimarket.nodes.node_projection_prod_promotion_gate
-- Version: 1.0.0
--
-- WHY THIS EXISTS
--   The runtime promotion gate refuses on twelve distinct branches and every
--   one of them was a RETURN VALUE -- no log, no raise, no metric. The typed
--   reason rode one bus event, was re-wrapped onto a second, and nothing in
--   the repository subscribed to either. A blocked promotion was therefore
--   indistinguishable from a promotion nobody requested: there was no row to
--   query, so "why did this not promote" had no answer surface at all. On
--   2026-09-20 the lab lane sat frozen for two and a quarter hours behind six
--   consecutive CORRECT refusals for exactly this reason.
--
-- WHY ONE ROW PER RUN AND NOT PER REFUSAL
--   The correlation IS the redeploy run, and a run's gate answer is one fact.
--   A redelivery of the same decision carries the same correlation and
--   converges on the row it already wrote. A corrected re-evaluation under
--   that correlation REPLACES the stale answer rather than sitting beside it,
--   because two contradictory rows for one run would make the surface
--   unreadable in exactly the way it is being built to fix.
--
-- WHY THE ALLOW PATH GETS A ROW
--   There is no allowed-filter in the writer and none here. A surface that
--   records only refusals cannot distinguish an allowed promotion from a gate
--   that never ran -- both read as an empty result. With a row on every
--   evaluation the two are a boolean apart, which is the whole of AC3.
--
-- WHY THE REQUESTED AND RESOLVED DIGESTS ARE TWO COLUMNS
--   On every refusal the gate resolves NO digest, so resolved_image_digest is
--   NULL exactly when the row matters most. Without the requested one beside
--   it a blocked row cannot say what was being promoted, which is one of the
--   four facts the acceptance criterion names.
--
-- WHY omninode_internal, EXPLICITLY QUALIFIED
--   omnibase_infra's scripts/ci/check_application_database_sql.py (OMN-15361)
--   rejects an UNQUALIFIED application relation target -- a bare CREATE
--   resolves against whatever search_path the runner carries -- and prohibits
--   `public` outright for application relations. No CREATE SCHEMA here: the
--   node-owned migration loop connects to the application database where
--   omninode_internal already exists, and a CREATE SCHEMA is exactly what
--   failed with "permission denied for database omnibase_infra" in OMN-16759.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.prod_promotion_gate_decisions (
    -- The redeploy run, and the key. A real UUID end to end on this chain:
    -- the orchestrator generates one per run and threads it onto the gate
    -- command, and OMN-18999 echoes it onto the decision so a consumer of the
    -- flat payload has a run identity at all. A decision that predates the
    -- echo falls back to the delivery's deterministic UUID-shaped identifier,
    -- which is weaker but still converges on redelivery.
    correlation_id           UUID        NOT NULL,

    -- The typed branch code: one value per return point of the gate. TEXT
    -- rather than an enum type because the vocabulary is owned by
    -- EnumProdGateOutcome in the application and a database enum would need a
    -- migration to add a branch -- which is how a new refusal ends up
    -- silently mapped onto an old token.
    --
    -- 'unknown' is a real value here. It means the decision predates the
    -- typed code AND carried no recoverable token in its reason; it is never
    -- a stand-in for a branch that was actually asserted.
    outcome                  TEXT        NOT NULL,

    -- Stored, not derived from outcome at read time. This is the column that
    -- makes an allow distinguishable from a never-ran.
    allowed                  BOOLEAN     NOT NULL,

    -- The gate's own sentence, verbatim. The outcome says WHICH branch fired;
    -- this says what it measured -- the digests it compared, the readiness
    -- state it found. Empty string rather than NULL so a reader never has to
    -- distinguish "no reason" from "column absent".
    reason                   TEXT        NOT NULL DEFAULT '',

    -- The authorization grant the decision was evaluated against. NULL is a
    -- real answer, not an unknown: it is the fact behind a
    -- missing_promotion_grant refusal.
    grant_id                 TEXT,

    -- What the promotion ASKED for.
    requested_image_digest   TEXT,

    -- What the gate RESOLVED. NULL on every refusal, by construction.
    resolved_image_digest    TEXT,

    -- Carried through the gate on every branch, including the refusals, so a
    -- rollback target is on the row that says the promotion did not happen.
    rollback_target          TEXT,

    -- From the echoed deploy context (OMN-16939). NULL when the decision rode
    -- through without one.
    runtime_lane             TEXT,
    promotion_batch_id       TEXT,

    -- The DETERMINISTIC evaluation time the grant resolver stamped -- event
    -- time, never an ingest clock, so a replay reproduces this row rather
    -- than re-dating it. The compute never calls now(), so this is the only
    -- evaluation clock the row can carry. NULL for a non-prod lane, where the
    -- gate is a no-op and no resolver ran.
    evaluated_at             TIMESTAMPTZ,

    -- Provenance: which topic the decision was read from.
    source_topic             TEXT        NOT NULL DEFAULT '',

    -- The one wall-clock value on the row, and deliberately not part of any
    -- verdict: it says when the reducer folded the event, never anything
    -- about the promotion.
    projected_at             TIMESTAMPTZ NOT NULL,

    -- Database-assigned monotonic page boundary. In the create migration
    -- rather than a follow-up because this table has never existed.
    projection_cursor        BIGSERIAL   NOT NULL,

    PRIMARY KEY (correlation_id)
);

-- SHAPE reconciliation, not merely existence (OMN-15376 class).
--
--   CREATE TABLE IF NOT EXISTS no-ops against a pre-existing table of the same
--   name, whatever shape it has. On a drifted database every column declared
--   above would silently not arrive, and the FIRST column-dependent statement
--   after it fails and takes the whole forward-migration run with it. One
--   guarded ADD COLUMN per declared column makes the create idempotent in
--   SHAPE and not merely in existence.
--
--   NOT NULL is deliberately absent from the ADDs that carry no DEFAULT: a
--   NOT NULL column added to a table that already has rows is refused by
--   Postgres. On a virgin database the CREATE above already applied the
--   constraint; on a drifted one an added column is nullable and the row is
--   visibly incomplete, which is the honest outcome. The primary key likewise
--   belongs to the CREATE and is not re-asserted here.
ALTER TABLE omninode_internal.prod_promotion_gate_decisions
    ADD COLUMN IF NOT EXISTS correlation_id         UUID;
ALTER TABLE omninode_internal.prod_promotion_gate_decisions
    ADD COLUMN IF NOT EXISTS outcome                TEXT;
ALTER TABLE omninode_internal.prod_promotion_gate_decisions
    ADD COLUMN IF NOT EXISTS allowed                BOOLEAN;
ALTER TABLE omninode_internal.prod_promotion_gate_decisions
    ADD COLUMN IF NOT EXISTS reason                 TEXT DEFAULT '';
ALTER TABLE omninode_internal.prod_promotion_gate_decisions
    ADD COLUMN IF NOT EXISTS grant_id               TEXT;
ALTER TABLE omninode_internal.prod_promotion_gate_decisions
    ADD COLUMN IF NOT EXISTS requested_image_digest TEXT;
ALTER TABLE omninode_internal.prod_promotion_gate_decisions
    ADD COLUMN IF NOT EXISTS resolved_image_digest  TEXT;
ALTER TABLE omninode_internal.prod_promotion_gate_decisions
    ADD COLUMN IF NOT EXISTS rollback_target        TEXT;
ALTER TABLE omninode_internal.prod_promotion_gate_decisions
    ADD COLUMN IF NOT EXISTS runtime_lane           TEXT;
ALTER TABLE omninode_internal.prod_promotion_gate_decisions
    ADD COLUMN IF NOT EXISTS promotion_batch_id     TEXT;
ALTER TABLE omninode_internal.prod_promotion_gate_decisions
    ADD COLUMN IF NOT EXISTS evaluated_at           TIMESTAMPTZ;
ALTER TABLE omninode_internal.prod_promotion_gate_decisions
    ADD COLUMN IF NOT EXISTS source_topic           TEXT DEFAULT '';
ALTER TABLE omninode_internal.prod_promotion_gate_decisions
    ADD COLUMN IF NOT EXISTS projected_at           TIMESTAMPTZ;
ALTER TABLE omninode_internal.prod_promotion_gate_decisions
    ADD COLUMN IF NOT EXISTS projection_cursor      BIGSERIAL;

-- "What refused recently, and why" — the question the whole surface exists to
-- answer, so it is an index scan rather than a scan of the whole table.
CREATE INDEX IF NOT EXISTS idx_prod_promotion_gate_outcome_time
    ON omninode_internal.prod_promotion_gate_decisions
    (outcome, projected_at DESC);

-- "Did this promotion batch ever get through, and what stopped it" — the
-- operator's own grouping when a lane sits frozen behind repeated refusals.
CREATE INDEX IF NOT EXISTS idx_prod_promotion_gate_batch_time
    ON omninode_internal.prod_promotion_gate_decisions
    (promotion_batch_id, projected_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_prod_promotion_gate_cursor
    ON omninode_internal.prod_promotion_gate_decisions (projection_cursor);
