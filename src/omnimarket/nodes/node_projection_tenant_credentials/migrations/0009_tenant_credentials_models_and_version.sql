-- OMN-21217: a credential row carries the key's own ordered model choices and
-- a version.
--
-- models: the customer's ordered model list for this key. Routing binds a
-- pooled key to its own first choice and refuses a tenant key whose choices it
-- cannot read, so the list lives on the key, not only on the tenant's route.
-- NULL means the choice is unknown; it is never a fallback to another key's.
--
-- version: the projection publishes each committed row to
-- onex.evt.omnimarket.tenant-credential-state.v1 and applies rows from it
-- only when their version is at least the stored one. Every register, model
-- change and revoke raises the version by one, so a row read back from the
-- topic after a newer write is ignored rather than undoing that write.
-- Existing rows start at 1, the version a fresh insert gets.
ALTER TABLE tenant_inference_credentials
    ADD COLUMN IF NOT EXISTS models JSONB,
    ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 1;

ALTER TABLE tenant_inference_credentials
    DROP CONSTRAINT IF EXISTS tenant_inference_credentials_models_array;
ALTER TABLE tenant_inference_credentials
    ADD CONSTRAINT tenant_inference_credentials_models_array
        CHECK (models IS NULL OR jsonb_typeof(models) = 'array');

ALTER TABLE tenant_inference_credentials
    DROP CONSTRAINT IF EXISTS tenant_inference_credentials_version_positive;
ALTER TABLE tenant_inference_credentials
    ADD CONSTRAINT tenant_inference_credentials_version_positive CHECK (version > 0);

-- Backfill an existing key's choice from the route that names it, only when
-- every live route naming that key for its provider agrees on one model. A key
-- named by routes that disagree, or by none, keeps NULL. The route's model_name
-- is read rather than its models list so this does not depend on the order the
-- two nodes' migrations run in. The route table may be absent on a database
-- that never ran the routing reducer's migrations; then there is nothing to
-- recover.
DO $$
BEGIN
    IF to_regclass('delegation_routing_tenant_overlay') IS NULL THEN
        RETURN;
    END IF;
    UPDATE tenant_inference_credentials c
    SET models = jsonb_build_array(chosen.model_name)
    FROM (
        SELECT o.tenant_id, o.secret_ref, o.provider, MIN(o.model_name) AS model_name
        FROM delegation_routing_tenant_overlay o
        WHERE o.secret_ref IS NOT NULL
        GROUP BY o.tenant_id, o.secret_ref, o.provider
        HAVING COUNT(DISTINCT o.model_name) = 1
           AND COUNT(*) = COUNT(o.model_name)
    ) chosen
    WHERE c.models IS NULL
      AND c.tenant_id = chosen.tenant_id
      AND c.api_key_ref = chosen.secret_ref
      AND c.provider = chosen.provider
      AND btrim(chosen.model_name) <> ''
      AND chosen.model_name <> 'byok-model-unresolved';
END
$$;
