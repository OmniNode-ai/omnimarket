-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
--
-- OMN-24286: row level security by tenant on the experiment arm results table. The policy
-- has no default-tenant fallback: with app.tenant_id unset the predicate is NULL and no row
-- is visible. The dashboard reader holds SELECT only.

ALTER TABLE public.experiment_arm_results FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.experiment_arm_results;
CREATE POLICY tenant_isolation ON public.experiment_arm_results
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.experiment_arm_results TO app_dashboard;
