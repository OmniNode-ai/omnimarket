-- OMN-20613 (agent-payments 402 plan, T0.6): make the ceiling budget-state
-- write atomic and replay-safe.
--
-- The writers of delegation_budget_state read the period row, added the event's
-- drawdown in the process, and wrote the sum back. Two concurrent writers both
-- read the same total and one drawdown was lost; a replay of event A after
-- event B double-counted because the only guard was last_correlation_id (the
-- LAST event, so A-B-A applied A twice); and last_event_at was overwritten by
-- whichever event arrived last, not the one that happened last.
--
-- This table records each applied event's identity. The writer inserts the
-- identity first and, only when that insert took a row, runs ONE
-- INSERT ... ON CONFLICT DO UPDATE on delegation_budget_state that increments
-- the totals inside the database, recomputes headroom, and keeps the greater
-- last_event_at. Both statements run in one transaction, so a failure rolls
-- the identity back with the totals.
--
-- Idempotent CREATE so warm dev/stability volumes reconcile cleanly.

CREATE TABLE IF NOT EXISTS delegation_budget_applied_events (
    tenant_id TEXT NOT NULL,
    cost_tier_name TEXT NOT NULL,
    budget_period TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, cost_tier_name, budget_period, correlation_id)
);

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
