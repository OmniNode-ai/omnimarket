-- OMN-15503: durable typed terminal outcome on delegation_events.
--
-- The 2026-07-29 delegation matrix lost 5 of 13 classes to provider quota
-- exhaustion, and the durable projection could not say so. The terminal event
-- carries a typed attempt ladder and the producer-side wire DTO
-- (omnibase_core 0.46.8 ModelDelegationResult.terminal_failure_cause, typed by
-- EnumDelegationTerminalFailureCause) already speaks a machine-readable cause,
-- but the consumer side dropped both: a forced-429 command projected as
-- quality_gate_passed=true with quality_gate_detail='completed', because the
-- outer delegate-skill-completed terminal arrived LAST and won the
-- correlation_id UPSERT.
--
-- Three columns close that seam:
--
--   terminal_ok            -- authoritative outer outcome reduced from the
--                             attempt ladder, NOT the declared status. False
--                             whenever no attempt produced an accepted answer.
--   terminal_failure_cause -- EnumDelegationTerminalFailureCause value
--                             ('provider_quota_exhausted' today). NULL means
--                             "no typed cause resolved"; it is never a claim
--                             of success -- read terminal_ok for that.
--   attempt_history        -- the typed per-tier ladder, so "refused after N
--                             escalations" is provable from the durable row
--                             rather than from a capture log.
--
-- All three are nullable with no default: existing rows predate the reduction
-- and must stay honestly unclassified rather than be backfilled into a claim
-- the event stream never made. PostgresSyncProjectionAdapter.upsert() builds
-- its INSERT column list from row.keys(), so these must exist before the
-- OMN-15503 handler change writes them; the in-memory dict adapter used by
-- unit tests has no schema to violate and would mask the gap.

ALTER TABLE tenant.delegation_events
    ADD COLUMN IF NOT EXISTS terminal_ok BOOLEAN;

ALTER TABLE tenant.delegation_events
    ADD COLUMN IF NOT EXISTS terminal_failure_cause TEXT;

ALTER TABLE tenant.delegation_events
    ADD COLUMN IF NOT EXISTS attempt_history JSONB;

-- Partial index: quota-exhaustion forensics scan only the failed tail, which
-- is a small minority of rows. A full index would be mostly NULLs.
CREATE INDEX IF NOT EXISTS idx_delegation_events_terminal_failure_cause
    ON tenant.delegation_events (terminal_failure_cause)
    WHERE terminal_failure_cause IS NOT NULL;
