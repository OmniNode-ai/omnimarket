-- OMN-19721: make occurrence accumulation idempotent across broker redelivery.
--
-- The projection row is keyed by fingerprint because many distinct runtime
-- error events intentionally collapse into one ranked row. Delivery identity
-- is separate: RuntimeLogEventBridge assigns event_id once, and a retry of that
-- event must contribute occurrence_count_local only once. The writer stores the
-- most recently applied event id and gates the SQL-side increment on it.
--
-- Nullable is deliberate for rows written before this migration. Their next
-- event is distinct from NULL, applies once, and establishes the new invariant.

ALTER TABLE omninode_internal.runtime_error_fingerprints
    ADD COLUMN IF NOT EXISTS last_applied_event_id TEXT;
