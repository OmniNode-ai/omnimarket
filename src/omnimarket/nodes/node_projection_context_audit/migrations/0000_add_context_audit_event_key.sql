-- Adds the deterministic row key node_projection_context_audit upserts on.
--
-- public.context_audit_events is created by omnibase_infra migration 053 with a
-- BIGSERIAL id and no natural key, so the retired consumer module could only
-- INSERT and a replay wrote every event again. This node derives event_key from
-- the event's Kafka coordinates (topic, partition, offset); the writer's
-- INSERT ... ON CONFLICT (event_key) DO UPDATE then keeps one row per event.
--
-- Rows written by the retired module keep event_key NULL. A unique index treats
-- NULLs as distinct, so they stay as they are.
--
-- The table itself is NOT created here: omnibase_infra migration 053 owns it.
-- This file only extends it, and is safe to re-run.

ALTER TABLE public.context_audit_events
    ADD COLUMN IF NOT EXISTS event_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_context_audit_events_event_key
    ON public.context_audit_events (event_key);

COMMENT ON COLUMN public.context_audit_events.event_key IS
    'Deterministic key of the source event: sha256 of topic:partition:offset in UUID shape. NULL on rows written before node_projection_context_audit.';
