-- OMN-19790: FORCE ROW LEVEL SECURITY for delegation_eval_items.
--
-- SPLIT OUT OF 0000 DELIBERATELY, AND FENCED.
--   scripts/run-forward-migrations.sh refuses ANY new node migration applying
--   FORCE ROW LEVEL SECURITY unless its id appears in
--   docker/migrations/forward/fenced-node-migrations.yaml. This statement lived
--   in the same file that creates the table, so fencing that file would have
--   skipped the CREATE TABLE and broken the 0001 grants on a fresh database.
--   Same split node_hook_event_capture 0002 used. The fence entry is added in
--   the change that vendors this file, so the runner skips this migration and
--   records the skip; the table exists with ENABLE ROW LEVEL SECURITY and its
--   tenant_isolation policy from 0000 in the meantime.
--
-- The tenant_isolation policy is restated here on purpose. omnibase_infra's
-- migration RLS policy atomicity gate (RULE A) refuses any file that turns row
-- level security on for a relation without a CREATE POLICY on it in the same
-- file, so that a relation is never FORCE-RLS with no admitting rule. The
-- statement is byte-for-byte the policy 0000 creates, so it changes nothing.
--
-- Idempotent: FORCE is idempotent and the policy is dropped before it is created.
ALTER TABLE public.delegation_eval_items FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.delegation_eval_items;
CREATE POLICY tenant_isolation ON public.delegation_eval_items
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- The dashboard reader keeps SELECT on this relation. 0000 grants it; it is restated
-- here, idempotently, so that every file restating the tenant_isolation policy also
-- carries the app_dashboard SELECT grant (the OMN-14894 grant-coverage ratchet).
GRANT SELECT ON public.delegation_eval_items TO app_dashboard;
