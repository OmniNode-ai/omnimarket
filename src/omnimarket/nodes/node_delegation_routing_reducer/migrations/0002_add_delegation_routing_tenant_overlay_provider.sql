-- OMN-18079: factual provider provenance for tenant routing overlays.
--
-- This is deliberately a new forward migration.  The projection migration
-- runner records the checksum of 0001 after it has applied it, so modifying
-- 0001 would neither upgrade deployed databases nor pass checksum validation.
-- A nullable column preserves rows written before provenance was available;
-- the routing resolver rejects a missing provider for a newly selected route.
-- No destructive rollback is needed: this additive, nullable schema change is
-- safe to retain if the application rollout is rolled back.

ALTER TABLE delegation_routing_tenant_overlay
    ADD COLUMN IF NOT EXISTS provider TEXT;
