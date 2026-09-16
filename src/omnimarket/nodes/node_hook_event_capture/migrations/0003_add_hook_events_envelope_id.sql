-- OMN-17201: preserve the transport envelope UUID beside content identity.
--
-- event_id remains the durable SHA-256 content address. The gateway envelope
-- UUID is a separate delivery trace and stays nullable for rows written by
-- producers that did not carry an envelope. No backfill or uniqueness rule is
-- introduced: historical rows retain their honest absence of this trace.

ALTER TABLE public.hook_events
    ADD COLUMN IF NOT EXISTS envelope_id UUID;
