-- =============================================================================
-- MIGRATION: durable alert-channel liveness verdicts
-- =============================================================================
-- Owner:   omnimarket.nodes.node_projection_alert_channel_liveness
-- Version: 1.0.0
--
-- WHY THIS EXISTS
--   The checker publishes its verdict independently of the channel it judges,
--   but a published event with no recording subscriber remains unqueryable.
--   A dead Slack channel must leave a fact behind even when Slack cannot
--   deliver the alert that would otherwise have reported that failure.
--
-- WHY ONE ROW PER PROBED EVENT
--   The deterministic correlation comes from topic/partition/offset, with a
--   content-derived fallback for the bare adapter path. A redelivery of the
--   same event converges on its existing row, while a later probe retains its
--   own measurement. Throttled ticks make no measurement and write no row.
--
-- WHY STATUS AND HEALTH ARE BOTH STORED
--   Only LIVE proves health. DEAD, NOT_CONFIGURED and PROBE_ERROR retain their
--   different operator actions while all three store healthy=false. Health
--   is derived in the pure fold rather than trusted from an independent wire
--   flag, so a probe failure cannot silently become a healthy channel.
--
-- WHY checked_at AND projected_at ARE TWO COLUMNS
--   checked_at preserves event time when present. The current flat producer
--   result carries none, so the writer reads the envelope time; when neither
--   supplies one, checked_at equals the single projected_at clock value.
--   projected_at always says when this projection processed the verdict.
--
-- WHY omninode_internal, EXPLICITLY QUALIFIED
--   The application SQL ownership gate rejects unqualified relations and
--   prohibits public for application tables. The migration loop connects to
--   the application database where this schema already exists; creating a
--   schema here would require unrelated database-level privileges.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.alert_channel_liveness_verdicts (
    -- One measured event, and the redelivery key. A UUID end to end rather
    -- than a textual key that the writer and the relation interpret differently.
    correlation_id           UUID        NOT NULL,

    -- The vocabulary belongs to the application wire model. TEXT avoids a
    -- database enum migration whenever the producing vocabulary evolves.
    status                   TEXT        NOT NULL,
    healthy                  BOOLEAN     NOT NULL,

    -- The measurement's own explanation and optional Slack error token. The
    -- token remains separate so readers never need to parse the sentence.
    reason                   TEXT        NOT NULL DEFAULT '',
    slack_error              TEXT,

    -- The cadence actually used and whether the producer surfaced failure.
    -- These are measured producer facts, never projection configuration.
    probe_interval_seconds   INTEGER     NOT NULL,
    failure_surfaced         BOOLEAN     NOT NULL,

    -- Event time where available, otherwise the same value as projected_at.
    checked_at               TIMESTAMPTZ NOT NULL,
    source_topic             TEXT        NOT NULL DEFAULT '',

    -- Database-assigned monotonic boundary, retained by an upsert redelivery.
    projection_cursor        BIGSERIAL   NOT NULL,
    projected_at             TIMESTAMPTZ NOT NULL,

    PRIMARY KEY (correlation_id)
);

-- SHAPE reconciliation, not merely existence (the template's OMN-15376 rule).
-- CREATE TABLE IF NOT EXISTS does not reconcile an already-present relation.
-- Guarded ADDs ensure every declared column arrives before an index uses it.
-- NOT NULL is absent from ADDs without defaults because existing rows cannot
-- satisfy it. The virgin CREATE above already supplies those constraints;
-- drifted rows remain visibly incomplete rather than blocking migration.
ALTER TABLE omninode_internal.alert_channel_liveness_verdicts
    ADD COLUMN IF NOT EXISTS correlation_id         UUID;
ALTER TABLE omninode_internal.alert_channel_liveness_verdicts
    ADD COLUMN IF NOT EXISTS status                 TEXT;
ALTER TABLE omninode_internal.alert_channel_liveness_verdicts
    ADD COLUMN IF NOT EXISTS healthy                BOOLEAN;
ALTER TABLE omninode_internal.alert_channel_liveness_verdicts
    ADD COLUMN IF NOT EXISTS reason                 TEXT DEFAULT '';
ALTER TABLE omninode_internal.alert_channel_liveness_verdicts
    ADD COLUMN IF NOT EXISTS slack_error            TEXT;
ALTER TABLE omninode_internal.alert_channel_liveness_verdicts
    ADD COLUMN IF NOT EXISTS probe_interval_seconds INTEGER;
ALTER TABLE omninode_internal.alert_channel_liveness_verdicts
    ADD COLUMN IF NOT EXISTS failure_surfaced        BOOLEAN;
ALTER TABLE omninode_internal.alert_channel_liveness_verdicts
    ADD COLUMN IF NOT EXISTS checked_at             TIMESTAMPTZ;
ALTER TABLE omninode_internal.alert_channel_liveness_verdicts
    ADD COLUMN IF NOT EXISTS source_topic           TEXT DEFAULT '';
ALTER TABLE omninode_internal.alert_channel_liveness_verdicts
    ADD COLUMN IF NOT EXISTS projection_cursor      BIGSERIAL;
ALTER TABLE omninode_internal.alert_channel_liveness_verdicts
    ADD COLUMN IF NOT EXISTS projected_at           TIMESTAMPTZ;

-- Recent measurements by status, including failures of the probe itself.
CREATE INDEX IF NOT EXISTS idx_alert_channel_liveness_status_time
    ON omninode_internal.alert_channel_liveness_verdicts (status, checked_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_channel_liveness_cursor
    ON omninode_internal.alert_channel_liveness_verdicts (projection_cursor);
