-- =============================================================================
-- MIGRATION: runner-fleet liveness read model
-- =============================================================================
-- Ticket:  OMN-18768 (C1 of epic OMN-18767 — the lab observability tab)
-- Owner:   omnimarket.nodes.node_projection_runner_fleet
-- Version: 1.0.0
--
-- WHY THIS EXISTS
--   "What runners are running" had no producer and no read model anywhere. A
--   sweep of every projection snapshot topic across the runtime sources
--   returns 60+ topics and not one runner, lane, fleet or host topic. The two
--   surfaces that DO know the answer are the GitHub org runners REST API and
--   `docker ps` on the lab host; neither is a projection, and the dashboard
--   renders projections. OMN-16943 records the structural cause: the runner
--   monitor posts its findings to a chat channel and emits no bus event.
--
--   This table is the read model over the event that ticket's emitter half now
--   publishes.
--
-- WHY runner_name IS THE KEY
--   The exposure answers "what is each runner doing NOW", so a new observation
--   REPLACES the runner's previous row rather than appending. One row per
--   runner bounds the table by fleet size (~69) instead of by observation
--   frequency (one cycle every three minutes, forever). The full observation
--   history is not kept here and is deliberately not this table's job — a
--   time-series of fleet state is a different question with a different shape.
--
-- WHY THERE IS NO 'unknown' STATUS
--   An observation that could not read the fleet is never published (the
--   emitter refuses), so no row is ever written from a failed read. Admitting
--   an 'unknown' status would let a GitHub API blip materialize as ~69 rows
--   indistinguishable from a fleet that really is in an unknown state.
--
-- WHY observed_at IS NOT NULL AND IS THE STALE-WRITE GUARD
--   observed_at is PRODUCER-assigned event time. The upsert below refuses a
--   write whose observed_at is not newer than what is stored, in SQL rather
--   than read-then-write, so a redelivered older observation cannot overwrite
--   a newer one under concurrent consumers. An ingest clock would not do: two
--   consumers processing out of order both have "now" as their ingest time.
--
-- WHY omninode_internal, EXPLICITLY QUALIFIED
--   omnibase_infra's scripts/ci/check_application_database_sql.py (OMN-15361)
--   rejects an UNQUALIFIED application relation target (a bare CREATE resolves
--   against whatever search_path the runner carries) and prohibits `public`
--   outright. No CREATE SCHEMA here: the node-owned migration loop connects to
--   the application database where omninode_internal already exists; the flat
--   loop's CREATE SCHEMA is what failed with "permission denied for database"
--   in OMN-16759 and blocked every staging deploy.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.runner_fleet_liveness (
    runner_name        TEXT        NOT NULL,

    -- The GitHub-assigned runner id. Nullable because it is an identifier of
    -- convenience for cross-referencing the org API, never the row key: a
    -- deregistered-and-reregistered runner keeps its name and gets a new id.
    runner_id          BIGINT,

    -- The operational class: omnibase-ci / omnibase-verify / omnibase-deploy /
    -- omnibase-prod-deploy / omnibase-customer-plane, or 'unclassified'.
    -- 'self-hosted' is on every runner and is never chosen — it classifies
    -- nothing. Which class is down is the whole operational question.
    label_class        TEXT        NOT NULL,

    -- Every label verbatim, so a reader can answer a question the class does
    -- not cover without a second round trip to GitHub.
    labels             JSONB       NOT NULL DEFAULT '[]'::jsonb,

    -- Where the runner actually lives, from its `host-<id>` label
    -- (`host-101`, `host-105`, `host-201` in the live pool). Falls back to the
    -- observing host when the runner carries no such label.
    host               TEXT        NOT NULL,

    -- The machine that TOOK the observation. Two different facts: on
    -- 2026-09-18 the only offline runner in the org pool was on .105 while the
    -- observer was .201, and collapsing these onto the observer would point an
    -- operator at the wrong machine.
    --
    -- It is HALF the tombstone scope, never all of it. Alone it is unsound
    -- beside a single-column primary key: every upsert rewrites this column,
    -- so row ownership moves on every write and a deregistered runner can
    -- linger reporting online. Supersession (observed_at <) alone is unsound
    -- the other way: a narrower observer deletes runners it never saw. The
    -- scope is the CONJUNCTION; see the writer, which carries the record of
    -- being blocked once on each horn.
    observing_host     TEXT        NOT NULL,

    -- online | busy | offline. Constrained rather than free text: an emitter
    -- that started writing 'Online' would otherwise split the fleet in two on
    -- every panel that groups by status, silently.
    status             TEXT        NOT NULL,

    -- The job a BUSY runner is executing, when the emitter could resolve it.
    -- NULL on a busy runner means "executing something we could not name" --
    -- a different fact from idle, and from a fabricated id. The GitHub org
    -- runners API carries `busy` but no job identity, so NULL is the common
    -- case and is honest rather than a gap to fill in later.
    current_job_id     TEXT,

    -- Producer-assigned observation time. The ordering authority.
    observed_at        TIMESTAMPTZ NOT NULL,

    first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- OMN-18043: a database-assigned, unique monotonic value giving the
    -- projection API a stable page boundary. Separate from the business key.
    projection_cursor  BIGSERIAL   NOT NULL,

    CONSTRAINT pk_runner_fleet_liveness PRIMARY KEY (runner_name),
    CONSTRAINT ck_runner_fleet_liveness_status
        CHECK (status IN ('online', 'busy', 'offline'))
);

-- COLUMN RECONCILIATION (OMN-15376 class, enforced by omnibase_infra's
-- tests/ci/test_node_migration_shape_reconciliation.py).
--   `CREATE TABLE IF NOT EXISTS` NO-OPS against a pre-existing table of the
--   same name, whatever shape it has. On a database where an earlier or
--   drifted `runner_fleet_liveness` already exists, every column declared
--   above would silently not arrive, and the FIRST column-dependent statement
--   after it -- the unique index on projection_cursor, three lines down --
--   fails and takes the whole forward-migration run with it. One guarded ADD
--   COLUMN per declared column makes the create idempotent in SHAPE and not
--   merely in existence.
--
--   NOT NULL is deliberately absent from the ADDs that carry no DEFAULT: a
--   NOT NULL column added to a table that already has rows is refused by
--   Postgres. On a virgin database the CREATE above already applied the
--   constraint; on a drifted one an added column is nullable and the row is
--   visibly incomplete, which is the honest outcome. The CHECK and the primary
--   key likewise belong to the CREATE and are not re-asserted here.
ALTER TABLE omninode_internal.runner_fleet_liveness
    ADD COLUMN IF NOT EXISTS runner_name       TEXT;
ALTER TABLE omninode_internal.runner_fleet_liveness
    ADD COLUMN IF NOT EXISTS runner_id         BIGINT;
ALTER TABLE omninode_internal.runner_fleet_liveness
    ADD COLUMN IF NOT EXISTS label_class       TEXT;
ALTER TABLE omninode_internal.runner_fleet_liveness
    ADD COLUMN IF NOT EXISTS labels            JSONB DEFAULT '[]'::jsonb;
ALTER TABLE omninode_internal.runner_fleet_liveness
    ADD COLUMN IF NOT EXISTS host              TEXT;
ALTER TABLE omninode_internal.runner_fleet_liveness
    ADD COLUMN IF NOT EXISTS observing_host    TEXT;
ALTER TABLE omninode_internal.runner_fleet_liveness
    ADD COLUMN IF NOT EXISTS status            TEXT;
ALTER TABLE omninode_internal.runner_fleet_liveness
    ADD COLUMN IF NOT EXISTS current_job_id    TEXT;
ALTER TABLE omninode_internal.runner_fleet_liveness
    ADD COLUMN IF NOT EXISTS observed_at       TIMESTAMPTZ;
ALTER TABLE omninode_internal.runner_fleet_liveness
    ADD COLUMN IF NOT EXISTS first_seen_at     TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.runner_fleet_liveness
    ADD COLUMN IF NOT EXISTS updated_at        TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.runner_fleet_liveness
    ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_runner_fleet_liveness_projection_cursor
    ON omninode_internal.runner_fleet_liveness (projection_cursor);

-- "Which class is degraded" and "what is stale" are the two reads this table
-- exists to serve; both are index-backed rather than sequential scans that
-- happen to be fast at 69 rows.
CREATE INDEX IF NOT EXISTS idx_runner_fleet_liveness_class_status
    ON omninode_internal.runner_fleet_liveness (label_class, status);

CREATE INDEX IF NOT EXISTS idx_runner_fleet_liveness_observed_at
    ON omninode_internal.runner_fleet_liveness (observed_at DESC);

-- The writer reads the superseded set on every observation to name the
-- runners that disappeared -- `WHERE observed_at < $1 AND observing_host = $2`
-- -- so at fleet scale that is the hot path of the whole node. Composite,
-- leading on the column with the selectivity: at ~69 rows and one observer
-- `observing_host` alone selects everything, while `observed_at` narrows to
-- the rows a cycle actually supersedes.
CREATE INDEX IF NOT EXISTS idx_runner_fleet_liveness_superseded_scope
    ON omninode_internal.runner_fleet_liveness (observed_at, observing_host);

COMMENT ON TABLE omninode_internal.runner_fleet_liveness IS
    'OMN-18768: per-runner liveness for the self-hosted CI fleet, keyed on runner_name. '
    'Written only by node_projection_runner_fleet from onex.evt.omnibase-infra.runner-fleet.v1. '
    'A runner that disappears from an observation is DELETED and tombstoned, never '
    'left behind reporting online.';
