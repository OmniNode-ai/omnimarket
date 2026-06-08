-- OMN-12775 (close-the-loop A3): persist the generation proof fields the demo
-- acceptance criteria require directly on the generation_events row.
--
-- The omnidash render reads six proof fields off generation_events
-- (output_payload_sha256, contract_sha256, handler_sha256, routing_source,
-- resolved_endpoint, projection_owner). Migration 0012 added the full output
-- (contract_yaml + handler_source) but nothing produced these proof fields, so
-- the canonical projection API returned NULLs. Per audit wwmx7fobe/D6 the
-- decision is to POPULATE them in the write path — they ARE the evidence the
-- acceptance criteria require, so the columns must exist.
--
-- Field semantics (all populated by the projection write path):
--   - contract_sha256:        sha256(contract_yaml) hex digest
--   - handler_sha256:         sha256(handler_source) hex digest
--   - output_payload_sha256:  sha256(contract_yaml || handler_source) hex digest
--   - routing_source:         where the endpoint/model decision came from
--                             (contract / overlay / routing authority)
--   - resolved_endpoint:      the COMPLETE endpoint URL the routing authority
--                             resolved for this run (verbatim, no construction)
--   - projection_owner:       the canonical projection node that wrote the row
--
-- Provenance captured per-row for evidence packets:
--   - migration id: 0013
--   - source commit: tracked by git history on this file
--   - applied lane: recorded by the migration runner (stability-test / prod)
--
-- Empty string is the failed/incomplete-generation sentinel — never NULL, never
-- truncated. SHA256 of the FULL stored value is verifiable at query time via
-- encode(sha256(contract_yaml::bytea), 'hex') so the persisted digest can be
-- cross-checked without visual render.
--
-- Applied only (never reversed); deploy to live lanes is user-gated.

ALTER TABLE generation_events
    ADD COLUMN IF NOT EXISTS output_payload_sha256 TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS contract_sha256 TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS handler_sha256 TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS routing_source TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS resolved_endpoint TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS projection_owner TEXT NOT NULL DEFAULT '';
