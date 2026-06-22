-- OMN-13407: persist delegation context-pack identity on the canonical
-- delegation projection so context ON/OFF ROI is measurable from the
-- correlation-trace surface. Empty string is the OFF/no-context arm.

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS context_pack_hash TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_delegation_events_context_pack_hash
    ON delegation_events (context_pack_hash);
