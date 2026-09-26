-- OMN-19716: current broker topic-activity projection.
-- Owner: omnimarket.nodes.node_projection_topic_activity
-- Target database: omnidash_analytics; physical schema: omninode_internal.

CREATE TABLE IF NOT EXISTS omninode_internal.topic_activity (
    topic                                  TEXT        NOT NULL,
    sampled_at                             TIMESTAMPTZ,
    high_watermark_total                   BIGINT,
    low_watermark_total                    BIGINT,
    retained_messages                      BIGINT,
    messages_since_previous_sample         BIGINT,
    rate_per_second                        DOUBLE PRECISION,
    messages_last_hour                     BIGINT,
    messages_last_24h                      BIGINT,
    rate_last_hour_per_second              DOUBLE PRECISION,
    retention_truncated                    BOOLEAN,
    newest_message_at                      TIMESTAMPTZ,
    newest_message_age_seconds_at_sample   DOUBLE PRECISION,
    activity_state                         TEXT        NOT NULL DEFAULT 'UNKNOWN',
    updated_at                             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor                      BIGSERIAL   NOT NULL,

    CONSTRAINT pk_topic_activity PRIMARY KEY (topic),
    CONSTRAINT ck_topic_activity_state
        CHECK (activity_state IN ('ACTIVE', 'QUIET', 'UNKNOWN')),
    CONSTRAINT ck_topic_activity_watermarks
        CHECK (
            high_watermark_total IS NULL
            OR low_watermark_total IS NULL
            OR high_watermark_total >= low_watermark_total
        )
);

-- CREATE TABLE IF NOT EXISTS does not reconcile a drifted pre-existing table.
-- Keep the migration idempotent in shape with one guarded ADD per declaration.
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS topic TEXT;
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS sampled_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS high_watermark_total BIGINT;
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS low_watermark_total BIGINT;
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS retained_messages BIGINT;
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS messages_since_previous_sample BIGINT;
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS rate_per_second DOUBLE PRECISION;
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS messages_last_hour BIGINT;
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS messages_last_24h BIGINT;
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS rate_last_hour_per_second DOUBLE PRECISION;
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS retention_truncated BOOLEAN;
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS newest_message_at TIMESTAMPTZ;
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS newest_message_age_seconds_at_sample DOUBLE PRECISION;
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS activity_state TEXT DEFAULT 'UNKNOWN';
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.topic_activity
    ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_topic_activity_projection_cursor
    ON omninode_internal.topic_activity (projection_cursor);

CREATE INDEX IF NOT EXISTS idx_topic_activity_sampled_at
    ON omninode_internal.topic_activity (sampled_at DESC);

CREATE INDEX IF NOT EXISTS idx_topic_activity_rate_hour
    ON omninode_internal.topic_activity (rate_last_hour_per_second DESC);

COMMENT ON TABLE omninode_internal.topic_activity IS
    'OMN-19716: current per-topic broker watermarks, message rates, and newest-event age. '
    'Written only by node_projection_topic_activity from typed broker samples.';
