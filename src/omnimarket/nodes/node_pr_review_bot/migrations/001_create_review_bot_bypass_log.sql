-- Migration: Create review_bot_bypass_log table — OMN-8497
-- Target DB: omnidash_analytics (omnibase_infra postgres on .201:5436)
-- Node: node_pr_review_bot / HandlerEmergencyBypassParser

CREATE TABLE IF NOT EXISTS review_bot_bypass_log (
    audit_id          UUID PRIMARY KEY,
    pr_url            TEXT NOT NULL,
    actor             TEXT NOT NULL,
    reason            TEXT NOT NULL,
    bypass_timestamp  TIMESTAMPTZ NOT NULL,
    kafka_event_id    UUID NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rbl_actor ON review_bot_bypass_log (actor);
CREATE INDEX IF NOT EXISTS idx_rbl_pr_url ON review_bot_bypass_log (pr_url);
CREATE INDEX IF NOT EXISTS idx_rbl_bypass_timestamp ON review_bot_bypass_log (bypass_timestamp);
