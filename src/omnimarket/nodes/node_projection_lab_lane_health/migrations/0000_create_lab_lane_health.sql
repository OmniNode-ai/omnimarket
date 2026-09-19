-- =============================================================================
-- MIGRATION: one row per lab lane, three independently-aged contributing facts
-- =============================================================================
-- Ticket:  OMN-18769 (C2 of epic OMN-18767 — the lab observability tab)
-- Owner:   omnimarket.nodes.node_projection_lab_lane_health
-- Version: 1.0.0
--
-- WHY THIS EXISTS
--   The three facts a "what is working in the lab" panel needs are each real
--   today and each unreadable from a dashboard: the lane census is a committed
--   FILE, the runtime's health dimensions are a raw runtime internal on
--   /health, and a lab-pass verdict is a GitHub Actions ARTIFACT reachable
--   only by exact-name query. Three access models, none of them a projection.
--   This table is where they become one.
--
-- WHY EACH FACT HAS ITS OWN observed_at
--   Their natural ages differ by four orders of magnitude: the census is
--   hours-to-days old by construction, the runtime health is seconds old, and
--   a receipt is per-sha. A single row-level freshness column would let the
--   health fact make a two-day-old census look current — which is exactly the
--   dishonesty the archived topology view shipped, and exactly what OMN-18769
--   AC5 falsifies. Hence three observed_at columns, three pre-decay verdicts,
--   and no row-level updated_at that a reader could mistake for freshness.
--
-- WHY THE UPSERTS ARE PER-FACT AND GUARDED
--   Each fact kind arrives on its own topic, from its own producer, at its own
--   cadence. A whole-row read-modify-write would race three concurrent
--   consume loops against each other and let an older redelivery clobber a
--   newer fact. Instead each writer touches ONLY its own column group, under
--   an ON CONFLICT ... WHERE guard comparing observed_at — so ordering is
--   enforced in SQL rather than by a check-then-act the runtime cannot
--   serialize.
--
-- WHY omninode_internal, EXPLICITLY QUALIFIED
--   omnibase_infra's scripts/ci/check_application_database_sql.py (OMN-15361)
--   rejects an unqualified application relation target and prohibits `public`
--   outright. No CREATE SCHEMA: the node-owned migration loop connects to the
--   application database where omninode_internal already exists (OMN-16759).
--
-- WHY THERE IS NO drift/dimension CHILD TABLE
--   The drift items, the health dimensions and the failing check names are
--   display detail belonging to exactly one row, read only with that row, and
--   never queried across rows. JSONB holds them where they are read. A child
--   table would buy queryability nothing asks for and cost a join on the one
--   query this table exists to answer.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.lab_lane_health (
    -- compose-dev | onex-lab | onex-lab-k3s. The lab, and nothing else: the
    -- stability-test, judge and collaborator lanes are read-only surfaces and
    -- the reducer cannot key a row on one (OMN-18769 AC6).
    lane                      TEXT        NOT NULL,
    lane_class                TEXT        NOT NULL DEFAULT 'lab',

    -- --- fact 1: the lane census -------------------------------------------
    -- NULL observed_at means no census has ever been folded for this lane.
    -- That is UNKNOWN, and it is a different fact from drift_count = 0, which
    -- means a census RAN and found the lane clean. Defaulting either would
    -- collapse "nobody looked" into "looks fine".
    census_observed_at        TIMESTAMPTZ,
    census_original_status    TEXT,
    census_drift_count        INTEGER,
    census_drift_items        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    census_host               TEXT        NOT NULL DEFAULT '',

    -- --- fact 2: the runtime's own health verdict ---------------------------
    health_observed_at        TIMESTAMPTZ,
    health_original_status    TEXT,
    health_aggregate          TEXT        NOT NULL DEFAULT '',
    health_dimensions         JSONB       NOT NULL DEFAULT '[]'::jsonb,

    -- --- fact 3: the latest lab-pass receipt --------------------------------
    receipt_observed_at       TIMESTAMPTZ,
    receipt_original_status   TEXT,
    receipt_sha               TEXT        NOT NULL DEFAULT '',
    receipt_result            TEXT        NOT NULL DEFAULT '',
    receipt_failing_checks    JSONB       NOT NULL DEFAULT '[]'::jsonb,

    -- When the reducer last folded ANY event into this lane. Named
    -- projected_at, never updated_at, so no reader mistakes it for the age of
    -- the data: the ages are the three observed_at columns above.
    projected_at              TIMESTAMPTZ NOT NULL,

    PRIMARY KEY (lane)
);

-- The decayed verdicts are NOT stored. They are a pure function of
-- (original_status, observed_at, now) and storing them would mean a row whose
-- verdict is correct only at the instant it was written — the row would have
-- to be rewritten on a timer to stay true, and a timer that stops produces a
-- stale green. The three inputs are stored; every consumer derives the same
-- verdict from them with the shared decay function.
