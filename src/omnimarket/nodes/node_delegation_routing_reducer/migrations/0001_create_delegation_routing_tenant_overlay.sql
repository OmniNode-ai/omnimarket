-- OMN-15631 v1(a): per-tenant delegation routing overlay (dev/beta, NO RLS).
--
-- Design-feasibility assessment (ticket comment 41f99997) split AC2
-- (DB-enforced cross-tenant denial) into a follow-on ticket blocked on
-- OMN-14894/OMN-15356 (the `tenant`-schema RLS foundation), because that
-- foundation does not exist yet for this domain. This table intentionally
-- ships WITHOUT RLS: it is the v1(a) staging shape, additive so that once the
-- RLS foundation lands, promoting these rows into the RLS-backed `tenant.*`
-- home is a data MOVE (INSERT INTO tenant.delegation_routing_overlay SELECT
-- ... FROM tenant.delegation_routing_tenant_overlay), never a redesign. Do not add
-- RLS policies to this table directly -- that is explicitly out of scope for
-- v1(a) and belongs on the follow-on ticket.
--
-- Row shape and precedence semantics (AC6 seam, PR-body deliverable):
--   - One row per (tenant_id, task_type). A tenant may override the fully
--     resolved BACKEND BINDING (endpoint_url, model_name, secret_ref,
--     timeout_ms, max_tokens) for a given task_type -- WHOLESALE, not a
--     per-field deep-merge against the platform default. Mixing a tenant's
--     endpoint_url with the platform's model_name (or vice versa) would
--     silently address the wrong provider with the wrong model id, so a
--     matching overlay row fully REPLACES the platform-resolved backend for
--     that (tenant_id, task_type) pair rather than merging field-by-field.
--   - Routing STRUCTURE -- which tiers exist, tier_order, escalation policy,
--     pricing ceilings, cloud_routing_policy -- remains PLATFORM-FIXED in
--     v1(a) and is NOT represented in this table. A tenant overlay row is
--     consulted BEFORE tier/contract selection runs for that task_type: when
--     present it short-circuits tier iteration entirely (the tenant has
--     opted into a specific backend for that task class, not into the
--     platform's escalation ladder). When absent, resolution falls through
--     unchanged to the existing platform-default tier/contract logic --
--     this is what keeps AC3 (no-overlay resolves platform default) and AC4
--     (tenant-zero unchanged) true by construction, not by extra branching.
--   - secret_ref is nullable (an unauthenticated backend is a valid
--     configuration) but when present is resolved ONLY at the effect
--     boundary via ProtocolSecretStore, fail-fast on a miss -- this table
--     never stores a secret VALUE, only the ref name.
--   - No api_key_env column: the house env-var fallback
--     (BifrostBackendRef.api_key_env) is an OmniNode-house-deployment
--     convention. Tenant-overlay-sourced backends must never silently fall
--     back to a house key when their own ref is unresolved -- omitting the
--     column is what makes that impossible to wire in by accident.
--
-- tenant_id is TEXT (not UUID), matching the landed convention on every other
-- tenant_id column in this repo today (delegation_events 0022, llm_cost_
-- aggregates 0002, etc.) -- see omnimarket.projection.tenant_isolation
-- (HOUSE_TENANT_SLUG = 'omninode'). No FK to a `tenant` schema table: that
-- schema/RLS foundation is exactly what OMN-14894/OMN-15356 build, and this
-- table must not preempt or collide with OMN-15354's schema classification.
--
-- WHY SCHEMA-QUALIFIED tenant.delegation_routing_tenant_overlay, WITHOUT RLS
--   scripts/ci/check_application_database_sql.py (OMN-15361/OMN-15423)
--   rejects NEW deployable application relations that are bare/public.
--   The historical tenant-domain tables that still live physically in public
--   are grandfathered by the gate's frozen shrink-only baseline; this new
--   table is not grandfathered and must use the topology-declared tenant
--   schema from birth.
--
--   This migration deliberately ASSERTS the tenant schema precondition instead
--   of creating it. Schema provisioning belongs to the topology/application-ACL
--   layer; node-owned migrations fail loudly if their declared application
--   schema is absent on the target lane. OMN-16314 still owns the later RLS
--   promotion/foundation work; this file only stops adding a brand-new
--   tenant-domain table to public.

SELECT 1 / count(*) AS tenant_schema_exists_precondition
  FROM pg_catalog.pg_namespace
 WHERE nspname = 'tenant';

CREATE TABLE IF NOT EXISTS tenant.delegation_routing_tenant_overlay (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    backend_id TEXT NOT NULL,
    endpoint_url TEXT NOT NULL,
    model_name TEXT NOT NULL,
    secret_ref TEXT,
    timeout_ms INTEGER,
    max_tokens INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT delegation_routing_tenant_overlay_tenant_task_uq
        UNIQUE (tenant_id, task_type)
);

CREATE INDEX IF NOT EXISTS idx_delegation_routing_tenant_overlay_tenant_id
    ON tenant.delegation_routing_tenant_overlay (tenant_id);
