-- OMN-14894 (tranche 2): re-key projection_delegation_inference_response_text
-- by tenant instead of a single global singleton.
--
-- CONFIRMED ACTIVE LEAK (Linear OMN-14894 comment 6b84daf0, 2026-07-22): the
-- table is a hardcoded singleton (singleton_key CHECK ('global') from
-- migration 0001) -- every inference-response event, from every tenant,
-- upserts the SAME row. When two tenants share a correlation_id (or simply
-- both produce inference-response events), the second write's
-- latest_generated_text/latest_* fields silently overwrite the first
-- tenant's data on the one global row. This is not hypothetical: it is the
-- current, active write behavior of HandlerProjectionDelegationInferenceResponse.
--
-- ModelInferenceResponseData.tenant_id (OMN-14280) already carries the
-- owning tenant on the wire, echoed back from the inference intent -- but
-- the handler drops it entirely; it is never read out of the payload. This
-- migration and the accompanying handler change (this tranche) stop
-- dropping it:
--
--   1. Add tenant_id TEXT NOT NULL DEFAULT 'omninode' (same interim
--      single-tenant convention as delegation_events 0022 /
--      delegation_budget_state 0019).
--   2. Drop the CHECK (singleton_key = 'global') constraint -- the table is
--      no longer a single global singleton; it becomes one row PER TENANT,
--      keyed by tenant_id.
--   3. Re-key the existing 'global' row (if present) to the default tenant
--      identity so it survives the migration as that tenant's row, instead
--      of being silently orphaned or dropped.
--   4. Add a supporting index on tenant_id (singleton_key already carries a
--      PK index and now doubles as the per-tenant key value; a tenant_id
--      index keeps the two aligned in every plan).
--
-- The writer (this tranche) sets singleton_key := tenant_id on every upsert,
-- so future rows are naturally keyed one-per-tenant, and the existing
-- projection-API `limit: 1` read pattern becomes tenant-correct once the RLS
-- policy in 0003 scopes visibility to the requesting tenant's GUC -- the
-- same mechanism, not a special case, as the tranche-1 tables.

-- NOTE: Postgres truncates auto-generated constraint names to 63 bytes, so
-- the unnamed CHECK from 0001 is NOT
-- "projection_delegation_inference_response_text_singleton_key_check" --
-- verified live (stability-test lane, 2026-07-26) as:
--   projection_delegation_inference_response_te_singleton_key_check
ALTER TABLE projection_delegation_inference_response_text
    DROP CONSTRAINT IF EXISTS projection_delegation_inference_response_te_singleton_key_check;

ALTER TABLE projection_delegation_inference_response_text
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'omninode';

-- Re-key the pre-existing global singleton row (from 0001's seed INSERT, or
-- any row written before this migration) onto the default tenant identity.
UPDATE projection_delegation_inference_response_text
SET tenant_id = 'omninode',
    singleton_key = 'omninode'
WHERE singleton_key = 'global';

CREATE INDEX IF NOT EXISTS idx_projection_delegation_inference_response_text_tenant_id
    ON projection_delegation_inference_response_text (tenant_id);
