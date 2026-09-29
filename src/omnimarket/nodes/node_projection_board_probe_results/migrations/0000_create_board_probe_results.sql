-- OMN-19937: durable board probe results, one row per probe execution.
CREATE TABLE IF NOT EXISTS omninode_internal.board_probe_results (
    projection_cursor BIGSERIAL   NOT NULL,
    check_id          TEXT        NOT NULL,
    subject_kind      TEXT        NOT NULL,
    subject           TEXT        NOT NULL,
    repo              TEXT        NOT NULL,
    sha               TEXT        NOT NULL,
    surface_instance  TEXT        NOT NULL,
    execution_id      TEXT        NOT NULL,
    outcome           TEXT        NOT NULL,
    reasons           JSONB       NOT NULL DEFAULT '[]'::jsonb,
    evidence_items    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    finished_at       TIMESTAMPTZ NOT NULL,
    source_offset     BIGINT      NOT NULL,
    projected_at      TIMESTAMPTZ NOT NULL,

    PRIMARY KEY (
        check_id,
        subject_kind,
        repo,
        sha,
        surface_instance,
        execution_id
    ),
    CONSTRAINT board_probe_results_outcome_check
        CHECK (outcome IN ('PASS', 'FAIL', 'INDETERMINATE'))
);

-- OMN-15376 shape reconciliation: CREATE TABLE IF NOT EXISTS alone is not a
-- shape guarantee when a drifted relation already exists.
ALTER TABLE omninode_internal.board_probe_results
    ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;
ALTER TABLE omninode_internal.board_probe_results
    ADD COLUMN IF NOT EXISTS check_id TEXT;
ALTER TABLE omninode_internal.board_probe_results
    ADD COLUMN IF NOT EXISTS subject_kind TEXT;
ALTER TABLE omninode_internal.board_probe_results
    ADD COLUMN IF NOT EXISTS subject TEXT;
ALTER TABLE omninode_internal.board_probe_results
    ADD COLUMN IF NOT EXISTS repo TEXT;
ALTER TABLE omninode_internal.board_probe_results
    ADD COLUMN IF NOT EXISTS sha TEXT;
ALTER TABLE omninode_internal.board_probe_results
    ADD COLUMN IF NOT EXISTS surface_instance TEXT;
ALTER TABLE omninode_internal.board_probe_results
    ADD COLUMN IF NOT EXISTS execution_id TEXT;
ALTER TABLE omninode_internal.board_probe_results
    ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE omninode_internal.board_probe_results
    ADD COLUMN IF NOT EXISTS reasons JSONB DEFAULT '[]'::jsonb;
ALTER TABLE omninode_internal.board_probe_results
    ADD COLUMN IF NOT EXISTS evidence_items JSONB DEFAULT '[]'::jsonb;
ALTER TABLE omninode_internal.board_probe_results
    ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.board_probe_results
    ADD COLUMN IF NOT EXISTS source_offset BIGINT;
ALTER TABLE omninode_internal.board_probe_results
    ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS idx_board_probe_results_cursor
    ON omninode_internal.board_probe_results (projection_cursor);

CREATE INDEX IF NOT EXISTS idx_board_probe_results_latest_subject
    ON omninode_internal.board_probe_results (
        check_id,
        subject_kind,
        subject,
        repo,
        sha,
        surface_instance,
        finished_at DESC,
        source_offset DESC
    );
