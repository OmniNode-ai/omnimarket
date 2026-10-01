-- OMN-20154: FORCE ROW LEVEL SECURITY for provider_quota_state.
--
-- SPLIT OUT OF 0000 DELIBERATELY, AND FENCED, for the same reason
-- node_projection_delegation_eval 0002 is: scripts/run-forward-migrations.sh
-- refuses a new node migration applying FORCE ROW LEVEL SECURITY unless its id
-- is listed in docker/migrations/forward/fenced-node-migrations.yaml, and
-- fencing the file that creates the table would skip the CREATE TABLE and break
-- the 0001 grants on a fresh database. The fence entry is added in the change
-- that vendors this file; until the fence lifts, the table carries ENABLE ROW
-- LEVEL SECURITY and its tenant_isolation policy from 0000.
--
-- The tenant_isolation policy is restated on purpose (the omnibase_infra
-- migration RLS policy atomicity gate, RULE A). It is byte-for-byte the policy
-- 0000 creates, so it changes nothing.
--
-- Idempotent: FORCE is idempotent and the policy is dropped before it is created.
ALTER TABLE public.provider_quota_state FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.provider_quota_state;
CREATE POLICY tenant_isolation ON public.provider_quota_state
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- The dashboard reader keeps SELECT on this relation (the OMN-14894
-- grant-coverage ratchet): every file restating the policy carries the grant.
GRANT SELECT ON public.provider_quota_state TO app_dashboard;
