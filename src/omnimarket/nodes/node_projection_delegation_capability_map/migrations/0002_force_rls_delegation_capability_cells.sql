-- OMN-20169: fenced separately from CREATE, as in the delegation-eval template.
-- The infra vendor change must register this file in fenced-node-migrations.yaml.
-- Restate the policy atomically with ENABLE/FORCE for the RLS policy gate.
ALTER TABLE public.delegation_capability_cells ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.delegation_capability_cells FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.delegation_capability_cells;
CREATE POLICY tenant_isolation ON public.delegation_capability_cells
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.delegation_capability_cells TO app_dashboard;
