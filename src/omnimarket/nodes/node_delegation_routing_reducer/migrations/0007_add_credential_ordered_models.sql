-- OMN-21217: retain customer model order and guard whole-list replacements.
ALTER TABLE delegation_routing_tenant_overlay
    ADD COLUMN IF NOT EXISTS models JSONB NOT NULL DEFAULT '[]'::JSONB,
    ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 1;

UPDATE delegation_routing_tenant_overlay
SET models = jsonb_build_array(model_name)
WHERE models = '[]'::JSONB
  AND model_name IS NOT NULL
  AND btrim(model_name) <> ''
  AND model_name <> 'byok-model-unresolved';

-- Drop before add, as 0009 does, so a replayed or partly applied run does not
-- fail on a constraint that already exists.
ALTER TABLE delegation_routing_tenant_overlay
    DROP CONSTRAINT IF EXISTS credential_route_version_positive;
ALTER TABLE delegation_routing_tenant_overlay
    ADD CONSTRAINT credential_route_version_positive CHECK (version > 0);

ALTER TABLE delegation_routing_tenant_overlay
    DROP CONSTRAINT IF EXISTS credential_route_models_array;
ALTER TABLE delegation_routing_tenant_overlay
    ADD CONSTRAINT credential_route_models_array CHECK (jsonb_typeof(models) = 'array');
