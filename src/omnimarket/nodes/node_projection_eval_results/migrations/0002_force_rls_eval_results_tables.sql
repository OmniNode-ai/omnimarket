-- OMN-24171: FORCE ROW LEVEL SECURITY for the four eval-results stores.
--
-- Split out of 0000 on purpose, as node_projection_delegation_eval 0002 is: the forward
-- migration runner refuses a new node migration that applies FORCE ROW LEVEL SECURITY unless
-- its id is in the fenced list, and fencing the file that creates the tables would skip the
-- CREATE TABLE. The policies are restated so a relation is never FORCE-RLS with no admitting
-- rule, byte for byte the policies 0000 creates.
ALTER TABLE public.eval_item_scores FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.eval_item_scores;
CREATE POLICY tenant_isolation ON public.eval_item_scores
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.eval_item_scores TO app_dashboard;

ALTER TABLE public.eval_item_traces FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.eval_item_traces;
CREATE POLICY tenant_isolation ON public.eval_item_traces
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.eval_item_traces TO app_dashboard;

ALTER TABLE public.eval_run_closes FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.eval_run_closes;
CREATE POLICY tenant_isolation ON public.eval_run_closes
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.eval_run_closes TO app_dashboard;

ALTER TABLE public.eval_run_rates FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.eval_run_rates;
CREATE POLICY tenant_isolation ON public.eval_run_rates
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.eval_run_rates TO app_dashboard;
