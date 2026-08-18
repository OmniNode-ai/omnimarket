-- OMN-14751: add agent_source to intent_classification_events.
--
-- agent_source carries dispatcher provenance ('claude' | 'cursor') so intents
-- originating from omnicursor are distinguishable from Claude Code ones
-- downstream. This is the projection half of the OMN-14749 parity seam; the
-- producer half (OMN-14750) landed the field on the wire in omnibase_core.
--
-- Nullable BY DESIGN, and deliberately kept out of any NOT NULL convergence:
-- every row projected before this column existed legitimately has no source,
-- and events emitted by producers that predate the field still must project.
-- A NULL here means "unknown provenance", which is a real and valid state --
-- not a defect to be backfilled away.
--
-- Added as a separate forward migration rather than an edit to
-- 0000_create_intent_classification_events.sql: that artifact's content SHA-256
-- is pinned in docker/migrations/forward/_ledger/application-migrations.tsv,
-- so editing it in place fails the forward runner's checksum gate on any
-- database that already applied it.
--
-- Idempotent ADD so warm dev/stability volumes reconcile cleanly.

ALTER TABLE omninode_internal.intent_classification_events
  ADD COLUMN IF NOT EXISTS agent_source TEXT;
