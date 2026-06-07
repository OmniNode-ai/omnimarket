-- OMN-12780 (Wave 1C): persist the generated output from node-generation-completed events.
--
-- The terminal event model (node_generation_consumer/models/model_generation.py)
-- carries contract_yaml and handler_source but migration 0008 did not include them,
-- so _project_generation_completed was silently dropping the output.
--
-- Provenance captured per-row for evidence packets:
--   - migration id: 0012
--   - source commit: tracked by git history on this file
--   - applied lane: recorded by the migration runner (dev/stability-test/prod)
-- SHA256 of each generated output is computed at query time via:
--   encode(sha256(contract_yaml::bytea), 'hex') and
--   encode(sha256(handler_source::bytea), 'hex')
-- so large fields are verifiable without visual render (rev 3.1 acceptance).
--
-- Applied only (never reversed); DO NOT apply to live lanes — Wave 3 deploy is user-gated.

ALTER TABLE generation_events
    ADD COLUMN IF NOT EXISTS contract_yaml TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS handler_source TEXT NOT NULL DEFAULT '';
