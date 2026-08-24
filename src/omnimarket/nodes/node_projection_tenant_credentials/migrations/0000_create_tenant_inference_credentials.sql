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

WITH missing_api_key_refs AS (
    SELECT
        ctid,
        row_number() OVER (ORDER BY ctid) AS reconciled_rank
    FROM tenant_inference_credentials
    WHERE api_key_ref IS NULL OR api_key_ref = ''
)
UPDATE tenant_inference_credentials AS credentials
SET api_key_ref = '__reconciled_missing_api_key_ref_' || missing_api_key_refs.reconciled_rank::TEXT
FROM missing_api_key_refs
WHERE credentials.ctid = missing_api_key_refs.ctid;

WITH duplicate_api_key_refs AS (
    SELECT
        ctid,
        api_key_ref,
        row_number() OVER (PARTITION BY api_key_ref ORDER BY ctid) AS duplicate_rank,
        row_number() OVER (ORDER BY api_key_ref, ctid) AS global_duplicate_rank
    FROM tenant_inference_credentials
)
UPDATE tenant_inference_credentials AS credentials
SET api_key_ref = credentials.api_key_ref || '__reconciled_duplicate_' || duplicate_api_key_refs.global_duplicate_rank::TEXT
FROM duplicate_api_key_refs
WHERE credentials.ctid = duplicate_api_key_refs.ctid
  AND duplicate_api_key_refs.duplicate_rank > 1;

UPDATE tenant_inference_credentials
SET
    tenant_id = COALESCE(NULLIF(tenant_id, ''), '__reconciled_missing_tenant_' || api_key_ref),
    name = COALESCE(NULLIF(name, ''), '__reconciled_missing_name_' || api_key_ref),
    provider = COALESCE(NULLIF(provider, ''), '__reconciled_missing_provider_' || api_key_ref),
    created_at = COALESCE(created_at, NOW());

ALTER TABLE tenant_inference_credentials
    ALTER COLUMN api_key_ref SET NOT NULL,
    ALTER COLUMN tenant_id SET NOT NULL,
    ALTER COLUMN name SET NOT NULL,
    ALTER COLUMN provider SET NOT NULL,
    ALTER COLUMN created_at SET DEFAULT NOW(),
    ALTER COLUMN created_at SET NOT NULL;

ALTER TABLE tenant_inference_credentials
    DROP CONSTRAINT IF EXISTS tenant_inference_credentials_pkey;
ALTER TABLE tenant_inference_credentials
    ADD CONSTRAINT tenant_inference_credentials_pkey PRIMARY KEY (api_key_ref);
-- ---- END OMN-15376 shape reconciliation: tenant_inference_credentials ----

CREATE INDEX IF NOT EXISTS idx_tenant_inference_credentials_tenant_id
    ON tenant_inference_credentials (tenant_id);
