-- OMN-11299: project delegate-skill terminal metrics into dashboard-visible columns.

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS quality_gate_detail TEXT,
    ADD COLUMN IF NOT EXISTS latency_ms INT,
    ADD COLUMN IF NOT EXISTS tokens_input INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS tokens_output INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS pricing_manifest_version INT NOT NULL DEFAULT 0;

UPDATE delegation_events
SET latency_ms = delegation_latency_ms
WHERE latency_ms IS NULL
  AND delegation_latency_ms IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_delegation_events_tokens_total
    ON delegation_events ((tokens_input + tokens_output));
