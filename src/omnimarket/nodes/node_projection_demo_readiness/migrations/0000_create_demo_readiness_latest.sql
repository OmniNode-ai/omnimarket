-- OMN-19861. One latest typed terminal observation per demo node.
-- Producer-owned event time plus stable envelope UUID is the ordering authority.

CREATE TABLE IF NOT EXISTS omninode_internal.demo_readiness_latest (
    node_id                  TEXT        PRIMARY KEY,
    run_id                   TEXT        NOT NULL,
    status                   TEXT        NOT NULL,
    dashboard_configuration  TEXT        NOT NULL,
    observed_at              TIMESTAMPTZ NOT NULL,
    source_event_id           UUID        NOT NULL,
    evidence_path            TEXT,
    dry_run                  BOOLEAN     NOT NULL,
    failure_count            INTEGER,
    demo_blocker_count       INTEGER,
    demo_degraded_count      INTEGER,
    total_finding_count      INTEGER,
    projection_cursor        BIGSERIAL   NOT NULL,
    CONSTRAINT ck_demo_readiness_node
        CHECK (node_id IN ('demo_rehearsal', 'demo_drift_detector')),
    CONSTRAINT ck_demo_readiness_status
        CHECK (status IN ('GREEN', 'DEGRADED', 'BROKEN', 'UNCONFIGURED', 'DRY_RUN')),
    CONSTRAINT ck_demo_readiness_configuration
        CHECK (dashboard_configuration IN ('CONFIGURED', 'UNCONFIGURED')),
    CONSTRAINT ck_demo_readiness_counts
        CHECK (failure_count >= 0 AND demo_blocker_count IS NULL
               AND demo_degraded_count IS NULL AND total_finding_count IS NULL
               OR failure_count IS NULL AND demo_blocker_count >= 0
               AND demo_degraded_count >= 0 AND total_finding_count >= 0),
    CONSTRAINT ck_demo_readiness_evidence
        CHECK ((dry_run AND evidence_path IS NULL)
               OR (NOT dry_run AND evidence_path IS NOT NULL)),
    CONSTRAINT ck_demo_readiness_unconfigured
        CHECK (dashboard_configuration <> 'UNCONFIGURED'
               OR status = 'UNCONFIGURED')
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_demo_readiness_projection_cursor
    ON omninode_internal.demo_readiness_latest (projection_cursor);

COMMENT ON TABLE omninode_internal.demo_readiness_latest IS
    'OMN-19861: one latest durable typed terminal status per demo readiness node.';
