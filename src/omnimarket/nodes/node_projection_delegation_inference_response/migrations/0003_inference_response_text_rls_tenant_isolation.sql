-- OMN-14894 (tranche 2): row-level tenant isolation for
-- projection_delegation_inference_response_text.
--
-- Migration 0002 in this same tranche re-keyed the table from a single
-- global singleton (one row for every tenant, confirmed active cross-tenant
-- leak -- Linear OMN-14894 comment 6b84daf0) to one row per tenant_id. This
-- migration turns tenant_id into an enforced boundary, mirroring
-- node_projection_delegation's tranche-1 migration 0023 exactly:
--
--   ENABLE + FORCE ROW LEVEL SECURITY
--   POLICY tenant_isolation: tenant_id = current_setting('app.tenant_id', true)
--   GRANT SELECT to app_dashboard (OMN-14899, omnibase_infra forward
--   migration 094)
--
-- This is also what makes the projection_api `limit: 1` read pattern
-- (contract.yaml) tenant-correct: once a caller's session sets
-- app.tenant_id, RLS narrows the table to that tenant's single row before
-- the LIMIT 1 applies, instead of returning whichever tenant wrote last.
--
-- SEAM DECISION -- tenant_id stays TEXT, policy compares TEXT (no ::uuid):
--   same reasoning as 0023 -- tenant_id values on this surface are TEXT
--   slugs ('omninode', ...), not UUIDs.
--
-- BLAST RADIUS -- FORCE constrains the table OWNER too:
--   identical caveat to 0023 -- writers connecting as the postgres
--   SUPERUSER on compose lanes bypass RLS regardless of FORCE.
--
-- Fail-closed: current_setting('app.tenant_id', true) is NULL when the GUC
-- is unset, the predicate is NULL, and zero rows are visible. No default-
-- tenant fallback exists in the policy by design.
--
-- Idempotent: guarded role check, ENABLE/FORCE are idempotent, policy is
-- DROP + CREATE, GRANT is idempotent.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_dashboard') THEN
    RAISE EXCEPTION
      'app_dashboard role missing — apply omnibase_infra forward migration '
      '094_create_app_dashboard_role.sql (OMN-14899) before this RLS '
      'migration. RLS grants without the constrained read role are the '
      'exact bypass this work exists to prevent.';
  END IF;
END;
$$;

GRANT USAGE ON SCHEMA public TO app_dashboard;

ALTER TABLE projection_delegation_inference_response_text ENABLE ROW LEVEL SECURITY;
ALTER TABLE projection_delegation_inference_response_text FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON projection_delegation_inference_response_text;
CREATE POLICY tenant_isolation ON projection_delegation_inference_response_text
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT ON projection_delegation_inference_response_text TO app_dashboard;
