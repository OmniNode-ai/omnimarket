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
-- The row-level-security step is split into 0057 (fenced on arrival, like the
-- other FORCE RLS steps); this file creates the table and grants the writer.
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

DO $require_tenant_projection_writer$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'tenant_projection_writer'
    ) THEN
        RAISE EXCEPTION
            'tenant_projection_writer role missing; apply flat migration 103 before node migrations';
    END IF;
END
$require_tenant_projection_writer$;

GRANT USAGE ON SCHEMA public TO tenant_projection_writer;
GRANT SELECT, INSERT, UPDATE ON delegation_budget_applied_events TO tenant_projection_writer;
