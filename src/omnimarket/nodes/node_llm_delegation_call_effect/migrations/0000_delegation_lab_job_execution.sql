-- OMN-20604: durable execution ownership and transition outbox.
-- Projection tables remain read models; they never arbitrate an agent launch.
-- Idempotency: CREATE TABLE IF NOT EXISTS plus one guarded ADD COLUMN per declared column, so a drifted pre-existing table converges.
CREATE TABLE IF NOT EXISTS omninode_internal.delegation_lab_jobs (
    job_id TEXT PRIMARY KEY,
    spec JSONB NOT NULL,
    owner_runtime TEXT NOT NULL,
    seq BIGINT NOT NULL DEFAULT 0 CHECK (seq >= 0),
    row_data JSONB,
    result JSONB,
    taken_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE omninode_internal.delegation_lab_jobs
    ADD COLUMN IF NOT EXISTS job_id         TEXT;
ALTER TABLE omninode_internal.delegation_lab_jobs
    ADD COLUMN IF NOT EXISTS spec           JSONB;
ALTER TABLE omninode_internal.delegation_lab_jobs
    ADD COLUMN IF NOT EXISTS owner_runtime  TEXT;
ALTER TABLE omninode_internal.delegation_lab_jobs
    ADD COLUMN IF NOT EXISTS seq            BIGINT DEFAULT 0;
ALTER TABLE omninode_internal.delegation_lab_jobs
    ADD COLUMN IF NOT EXISTS row_data       JSONB;
ALTER TABLE omninode_internal.delegation_lab_jobs
    ADD COLUMN IF NOT EXISTS result         JSONB;
ALTER TABLE omninode_internal.delegation_lab_jobs
    ADD COLUMN IF NOT EXISTS taken_at       TIMESTAMPTZ DEFAULT NOW();

CREATE TABLE IF NOT EXISTS omninode_internal.delegation_lab_job_outbox (
    job_id TEXT NOT NULL REFERENCES omninode_internal.delegation_lab_jobs(job_id),
    seq BIGINT NOT NULL CHECK (seq > 0),
    payload JSONB NOT NULL,
    published BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (job_id, seq)
);

ALTER TABLE omninode_internal.delegation_lab_job_outbox
    ADD COLUMN IF NOT EXISTS job_id         TEXT;
ALTER TABLE omninode_internal.delegation_lab_job_outbox
    ADD COLUMN IF NOT EXISTS seq            BIGINT;
ALTER TABLE omninode_internal.delegation_lab_job_outbox
    ADD COLUMN IF NOT EXISTS payload        JSONB;
ALTER TABLE omninode_internal.delegation_lab_job_outbox
    ADD COLUMN IF NOT EXISTS published      BOOLEAN DEFAULT FALSE;

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;
GRANT SELECT, INSERT, UPDATE ON omninode_internal.delegation_lab_jobs TO omninode_runtime;
GRANT SELECT, INSERT, UPDATE ON omninode_internal.delegation_lab_job_outbox TO omninode_runtime;
