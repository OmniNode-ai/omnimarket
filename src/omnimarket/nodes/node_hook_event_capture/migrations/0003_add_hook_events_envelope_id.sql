-- OMN-17201: persist the canonical bus envelope UUID on hook_events.
--
-- 0001 is already legacy-declared in omnibase_infra's application-migration
-- ledger and must stay byte-stable. The hook-ledger writer now preserves the
-- envelope UUID separately from the content-addressed event_id, so this
-- successor migration adds the nullable column without rewriting history.

ALTER TABLE hook_events
    ADD COLUMN IF NOT EXISTS envelope_id VARCHAR(64);
