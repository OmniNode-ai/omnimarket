-- OMN-19793: FORCE ROW LEVEL SECURITY for the two eval-run tables.
--
-- Split out of 0003 and 0004 and fenced, for the reason 0002 gives: the forward
-- runner refuses a new node migration applying FORCE ROW LEVEL SECURITY unless
-- its id is fenced, and a fence skips a whole file, so FORCE cannot share a file
-- with the CREATE TABLE. The tenant_isolation policies are restated byte for byte
-- (RULE A of the RLS policy atomicity gate), with the app_dashboard SELECT grant
-- (the OMN-14894 grant-coverage ratchet). Idempotent.

ALTER TABLE public.delegation_eval_item_verdicts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.delegation_eval_item_verdicts;
CREATE POLICY tenant_isolation ON public.delegation_eval_item_verdicts
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.delegation_eval_item_verdicts TO app_dashboard;

ALTER TABLE public.delegation_eval_results FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.delegation_eval_results;
CREATE POLICY tenant_isolation ON public.delegation_eval_results
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.delegation_eval_results TO app_dashboard;
