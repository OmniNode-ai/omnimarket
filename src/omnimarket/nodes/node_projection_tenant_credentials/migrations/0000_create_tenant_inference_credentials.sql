-- OMN-16316: BYOK inference-credential ref catalog.
--
-- Populated ONLY by this node's projection (credential-registered /
-- credential-revoked events, published by omnimarket.projection.credential_publisher
-- -- the gateway value->ref exchange). Never written by the gateway handler
-- directly (OMN-15800: the API-server process must not write DB directly).
--
-- Deliberately a NEW, parallel table -- NOT merged into
-- delegation_routing_tenant_overlay (OMN-15631). Seam decision recorded on
-- both OMN-16316 and OMN-15631 (comment 62f52dac): every overlay row is a
-- full, NOT-NULL backend binding (endpoint_url, model_name, backend_id
-- required); a bare credential (key + provider only) is not a full backend
-- binding, so it does not belong in that table. When a tenant configures a
-- task_type override, the overlay's secret_ref column is populated by
-- COPYING one api_key_ref value from this table at override-creation time --
-- never a live join.
--
-- tenant_id is TEXT, matching the landed convention on every other tenant_id
-- column in this repo (see omnimarket.projection.tenant_isolation,
-- HOUSE_TENANT_SLUG = 'omninode'; delegation_routing_tenant_overlay 0001
-- uses the identical convention). No RLS in this v1 -- same dev/beta-only
-- posture as delegation_routing_tenant_overlay, promotable later once the
-- tenant-schema RLS foundation (OMN-14894/OMN-15356) lands.
--
-- api_key_ref is the PRIMARY KEY: it is minted collision-safe
-- (cred_{tenant_id}_{provider}_{uuid4}) by credential_publisher.mint_api_key_ref
-- and is never caller-supplied, so it is safe as the natural key.

CREATE TABLE IF NOT EXISTS tenant_inference_credentials (
    api_key_ref TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);

-- ---- BEGIN OMN-15376 shape reconciliation: tenant_inference_credentials ----
-- CREATE TABLE IF NOT EXISTS silently no-ops against a drifted pre-existing
-- table; the guarded adds below converge such a table onto the shape
-- declared above (no-ops on the fresh-create path, since every column
-- already exists there). No DROP, no recreate, no TRUNCATE. Matches
-- delegation_routing_tenant_overlay's (node_delegation_routing_reducer/0001,
-- OMN-15631) own precedent.
ALTER TABLE tenant_inference_credentials ADD COLUMN IF NOT EXISTS api_key_ref TEXT;
ALTER TABLE tenant_inference_credentials ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE tenant_inference_credentials ADD COLUMN IF NOT EXISTS name TEXT;
ALTER TABLE tenant_inference_credentials ADD COLUMN IF NOT EXISTS provider TEXT;
ALTER TABLE tenant_inference_credentials ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE tenant_inference_credentials ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
-- ---- END OMN-15376 shape reconciliation: tenant_inference_credentials ----

CREATE INDEX IF NOT EXISTS idx_tenant_inference_credentials_tenant_id
    ON tenant_inference_credentials (tenant_id);
