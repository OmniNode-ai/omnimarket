-- =============================================================================
-- MIGRATION: durable definition-of-done verification runs
-- =============================================================================
-- Ticket:  OMN-18900 (decision 3 of the Jev typed-decision shadow plan)
-- Owner:   omnimarket.nodes.node_projection_dod_verdict
-- Version: 1.0.0
--
-- WHY THIS EXISTS
--   node_dod_verify produces a per-ticket, machine-readable, check-level
--   verdict and throws it away. On 2026-09-20 the whole durable population of
--   that verdict on this fleet was NINE ad-hoc JSON files in one developer's
--   scratch directory; every other verdict ever produced is gone. The table
--   named in the only comment that mentions one -- dod_verify_runs -- did not
--   exist: nothing created it and nothing wrote it. This migration creates it.
--
-- WHY ONE ROW PER RUN AND NOT PER TICKET
--   The metric this table serves is work-caused attempts until the definition
--   of done verifies true. That is a question about a ticket's HISTORY. A
--   table keyed on the ticket alone holds only the last answer and can never
--   be asked how many attempts preceded it, which would make the surface
--   useless for the one thing it is being built for.
--
-- WHY THE KEY IS THREE COLUMNS
--   ticket_id alone is not unique per run. correlation_id is generated per
--   invocation and is unique in practice, but the command-line entry point
--   accepts one from the caller, so two runs CAN share it. completed_at
--   closes that: the three together pin the run. A genuine redelivery carries
--   all three identically and converges on the row it already wrote; a
--   re-verification carries a later completion time and is a new row, which
--   is exactly the history the metric reads.
--
-- WHY THE OUTCOME IS STORED AND THE COUNTS ARE STORED BESIDE IT
--   outcome/outcome_refusal record what the done predicate said AT PROJECTION
--   TIME, so a later change to the rule shows up as a disagreement rather
--   than as a silent re-scoring of history. The counts are on the row too, so
--   the current rule stays re-runnable over stored rows -- the acceptance
--   criterion is that the outcome be computable from the row ALONE, and both
--   halves of that are here.
--
-- WHY EVERY TERMINAL STATUS GETS A ROW
--   There is no status filter in the writer and none here. A verdict surface
--   that exists only on success cannot distinguish a failure from a
--   verification nobody ran, and telling those apart is the whole reason to
--   have the surface.
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

CREATE TABLE IF NOT EXISTS omninode_internal.dod_verify_runs (
    -- The Linear ticket. TEXT and not a UUID: the standing identifier rule
    -- exempts ticket_id by name because it is an OMN-nnnn identifier. It is
    -- unvalidated on the producing model, so this relation is the first
    -- surface that would see a malformed value; the writer's model asserts a
    -- non-empty length and deliberately does not guess at the shape.
    ticket_id                 TEXT        NOT NULL,

    -- The verification run's correlation id. A real UUID end to end on this
    -- chain -- the command-line entry point parses it as one and generates
    -- one when absent -- so it is a UUID column rather than TEXT.
    correlation_id            UUID        NOT NULL,

    -- Event time on both, never an ingest clock: the row is a statement
    -- about the run, so a replay reproduces it rather than re-dating it.
    completed_at              TIMESTAMPTZ NOT NULL,
    started_at                TIMESTAMPTZ NOT NULL,

    -- pending | verified | failed | skipped | unresolved
    status                    TEXT        NOT NULL,

    -- Set only when status is unresolved, mirroring the pairing invariant the
    -- producing models enforce. NULL is the correct absence here: a run that
    -- reached a verdict HAS no cause, which is different from an empty one.
    unresolved_cause          TEXT,

    -- The class counts. Every one of them is a first-class typed field on the
    -- producing payload already; nothing here is derived from prose.
    total_checks              INTEGER     NOT NULL,
    verified_count            INTEGER     NOT NULL,
    failed_count              INTEGER     NOT NULL,
    skipped_count             INTEGER     NOT NULL,
    superseded_count          INTEGER     NOT NULL,

    -- Ran, exited zero, and proved nothing about this ticket. Carried because
    -- the plan's report prints the non-probative share beside the improvement
    -- it would produce, so a definition of done thinning out is visible in
    -- the same table as the improvement it would manufacture.
    non_probative_count       INTEGER     NOT NULL,

    -- Passed AND executed the claimed behaviour. The conjunct the eval
    -- metric's done predicate requires, and the one that was ZERO in the only
    -- payload the 2026-09-20 inventory could read.
    behavior_proving_count    INTEGER     NOT NULL,

    -- Read live state and asserted on it. Counted alongside the behaviour
    -- count, never added into it.
    readback_proving_count    INTEGER     NOT NULL DEFAULT 0,

    -- Synthetic overlay items excluded from the total.
    unbindable_overlay_count  INTEGER     NOT NULL DEFAULT 0,

    -- done | refused. The eval metric's verdict at projection time.
    outcome                   TEXT        NOT NULL,

    -- checks_failed | status_not_verified | no_checks_run |
    -- no_behavior_proving_check. Set exactly when outcome is 'refused'; NULL
    -- otherwise, because a done run has no reason to carry.
    outcome_refusal           TEXT,

    -- Empty string rather than NULL so a reader never has to distinguish
    -- "no message" from "column absent".
    error_message             TEXT        NOT NULL DEFAULT '',

    -- The one wall-clock value on the row, and deliberately not part of any
    -- verdict: it says when the reducer folded the event, never anything
    -- about the run.
    projected_at              TIMESTAMPTZ NOT NULL,

    -- Database-assigned monotonic page boundary. Added in the create
    -- migration rather than a follow-up because this table has never existed.
    projection_cursor         BIGSERIAL   NOT NULL,

    PRIMARY KEY (ticket_id, correlation_id, completed_at)
);

-- SHAPE reconciliation, not merely existence (OMN-15376 class).
--
--   CREATE TABLE IF NOT EXISTS no-ops against a pre-existing table of the
--   same name, whatever shape it has. On a database where an earlier or
--   drifted `dod_verify_runs` already exists -- and the name has been spelled
--   in a comment in the shared topics module for long enough that a hand-made
--   one is possible -- every column declared above would silently not arrive,
--   and the FIRST column-dependent statement after it fails and takes the
--   whole forward-migration run with it. One guarded ADD COLUMN per declared
--   column makes the create idempotent in SHAPE and not merely in existence.
--
--   NOT NULL is deliberately absent from the ADDs that carry no DEFAULT: a
--   NOT NULL column added to a table that already has rows is refused by
--   Postgres. On a virgin database the CREATE above already applied the
--   constraint; on a drifted one an added column is nullable and the row is
--   visibly incomplete, which is the honest outcome. The primary key likewise
--   belongs to the CREATE and is not re-asserted here.
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS ticket_id                TEXT;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS correlation_id           UUID;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS completed_at             TIMESTAMPTZ;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS started_at               TIMESTAMPTZ;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS status                   TEXT;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS unresolved_cause         TEXT;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS total_checks             INTEGER;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS verified_count           INTEGER;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS failed_count             INTEGER;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS skipped_count            INTEGER;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS superseded_count         INTEGER;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS non_probative_count      INTEGER;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS behavior_proving_count   INTEGER;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS readback_proving_count   INTEGER DEFAULT 0;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS unbindable_overlay_count INTEGER DEFAULT 0;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS outcome                  TEXT;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS outcome_refusal          TEXT;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS error_message            TEXT DEFAULT '';
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS projected_at             TIMESTAMPTZ;
ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS projection_cursor        BIGSERIAL;

-- "How many attempts has this ticket taken, and in what order" — the one
-- query the metric exists to ask, so it is an index scan rather than a scan
-- of the whole table.
CREATE INDEX IF NOT EXISTS idx_dod_verify_runs_ticket_time
    ON omninode_internal.dod_verify_runs
    (ticket_id, completed_at DESC);

-- "Which runs counted as done, and which refused for which reason" — the
-- report's own grouping, and the query that makes a thinning definition of
-- done visible as a rising no-behaviour-proving share.
CREATE INDEX IF NOT EXISTS idx_dod_verify_runs_outcome_time
    ON omninode_internal.dod_verify_runs
    (outcome, outcome_refusal, completed_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_dod_verify_runs_cursor
    ON omninode_internal.dod_verify_runs (projection_cursor);
