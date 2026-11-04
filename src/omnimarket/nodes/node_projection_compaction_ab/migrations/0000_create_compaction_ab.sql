-- =============================================================================
-- MIGRATION: compaction A/B live projection and per-build scoreboard series
-- =============================================================================
-- Owner:   omnimarket.nodes.node_projection_compaction_ab
-- One row per (experiment_id, session_id, boundary_uuid). Each column has one
-- writer (see data/live_measurement.yaml); a writer's columns only advance when
-- its (event_time, event_id) is newer than the stored one.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.compaction_ab_rows (
    experiment_id               TEXT NOT NULL,
    session_id                  TEXT NOT NULL,
    boundary_uuid               TEXT NOT NULL,
    arm                         TEXT,
    block_index                 INTEGER,
    carryover                   BOOLEAN,
    assignment_event_time       TIMESTAMPTZ,
    assignment_event_id         TEXT,
    pre_compaction_tokens       BIGINT,
    trigger                     TEXT,
    rulings_in_window           INTEGER,
    open_loops_at_boundary      INTEGER,
    covariates_event_time       TIMESTAMPTZ,
    covariates_event_id         TEXT,
    hl1_events                  INTEGER,
    hl2_acted_items             INTEGER,
    hl2_open_loop_items         INTEGER,
    incident_count              INTEGER,
    hold_scope_violations       INTEGER,
    s5_cost_tokens              BIGINT,
    s3_defects                  INTEGER,
    s3_merged_prs               INTEGER,
    scores_event_time           TIMESTAMPTZ,
    scores_event_id             TEXT,
    PRIMARY KEY (experiment_id, session_id, boundary_uuid)
);

-- One row per (candidate_id, node_commit); a build with no candidate_id is refused.
CREATE TABLE IF NOT EXISTS omninode_internal.compaction_ab_build_series (
    candidate_id                    TEXT NOT NULL CHECK (candidate_id <> ''),
    node_commit                     TEXT NOT NULL,
    split                           TEXT NOT NULL CHECK (split = 'calibration'),
    h1_recall                       DOUBLE PRECISION,
    acted_share                     DOUBLE PRECISION,
    planted_recall                  DOUBLE PRECISION,
    contradiction_rate              DOUBLE PRECISION,
    false_memory_rate               DOUBLE PRECISION,
    hold_and_scope_violation_rate   DOUBLE PRECISION,
    cost_ratio                      DOUBLE PRECISION,
    recorded_at                     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (candidate_id, node_commit)
);
