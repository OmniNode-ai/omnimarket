-- OMN-18079: backfill provider provenance onto overlay rows minted before 0004.
--
-- WHY THIS FILE EXISTS
--
--   0004 added `provider` as a NULLABLE column and backfilled nothing, on the
--   stated reasoning that a nullable column "preserves rows written before
--   provenance was available".  In the same change
--   handler_delegation_routing._decision_from_tenant_overlay began REFUSING
--   any overlay row whose provider is blank, with
--   ProtocolConfigurationError [ONEX_CORE_041_INVALID_CONFIGURATION].  Those
--   two halves together do not preserve the old rows -- they strand them.  A
--   tenant whose route was minted before 0004 stops routing entirely the
--   moment the new resolver rolls out, and nothing tells them why.
--
--   Observed live: on 2026-09-16T00:40:04Z, minutes after omnimarket 0.4.98
--   reached onex-dev, every one of the 33 rows in this table carried
--   provider IS NULL and the staging business proof (OMN-15256) terminalised
--   `failed` on
--     Tenant routing overlay 'byok-glm' has no declared provider provenance.
--
-- WHY THIS IS A FACT AND NOT AN INFERENCE
--
--   This table has exactly one writer -- node_projection_tenant_credentials'
--   _project_routing_overlay -- and it writes `backend_id` and `provider` from
--   the SAME configs/byok_provider_backends.v1.yaml entry
--   (resolve_byok_provider_backend(provider) -> backend.backend_id,
--   backend.provider).  The catalogue binds each provider to one backend_id
--   and the backend_id strings are deliberately namespaced per provider, so
--   over the rows this statement can touch, backend_id -> provider INVERTS a
--   declared binding.  It does not guess one.
--
--   tests/unit/delegation/test_omn18079_overlay_provider_backfill.py fails if
--   the pairs below stop matching the catalogue in either direction.
--
-- WHAT IT DELIBERATELY DOES NOT DO
--
--   * It does not touch a row that already carries a provider.  A stamped row
--     records the provenance the writer observed; restating it from a mapping
--     would convert a fact into a derivation.
--   * It has no default, no COALESCE and no ELSE branch.  A row naming a
--     backend_id the catalogue does not declare keeps provider IS NULL and the
--     resolver keeps refusing it -- fail-closed is the correct outcome there,
--     because such a route's provenance genuinely is not knowable.
--   * It does not move `updated_at`.  On this table that column means "when
--     the tenant's route was last (re)minted from their credential"; a schema
--     backfill is not a re-registration and must not read as one.
--
--   Re-runnable and additive: a second application matches nothing, because
--   the first one left no NULL it can bind.  No destructive rollback is
--   needed -- rolling the application back to a resolver that ignores the
--   column leaves these values harmlessly correct.

UPDATE delegation_routing_tenant_overlay AS o
SET provider = declared.provider
FROM (
    VALUES
        ('byok-openrouter', 'openrouter'),
        ('byok-glm', 'glm')
) AS declared(backend_id, provider)
WHERE o.provider IS NULL
  AND o.backend_id = declared.backend_id;
