-- OMN-20613: tenant row-level security for delegation_budget_applied_events
-- (the table is created by 0056). Split out so the create and the writer grant
-- apply on every lane while this step waits for its operator-sequenced release.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_dashboard') THEN
    RAISE EXCEPTION
      'app_dashboard role missing — apply omnibase_infra forward migration '
      '094_create_app_dashboard_role.sql (OMN-14899) before this RLS '
      'migration.';
  END IF;
END;
$$;

ALTER TABLE delegation_budget_applied_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE delegation_budget_applied_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON delegation_budget_applied_events;
CREATE POLICY tenant_isolation ON delegation_budget_applied_events
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT ON delegation_budget_applied_events TO app_dashboard;
