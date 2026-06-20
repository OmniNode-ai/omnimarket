-- OMN-13235: per-tenant ceiling budget-state surface (cap + consumption),
-- event-sourced from delegation-completed events, projection-readable.
--
-- A `budgeted` ceiling tier (EnumTierCostType.BUDGETED, OMN-13234) carries a
-- monthly_cap_usd: tokens served while headroom remains cost 0 cash and draw the
-- cap down; tokens past the cap bill overage. This table is the durable,
-- event-sourced state of that drawdown so the dashboard / API can show how much
-- of a tenant's monthly ceiling budget is consumed and how much headroom remains.
--
-- One row per (tenant_id, cost_tier_name, budget_period) — the period is the
-- UTC month (YYYY-MM) the cap applies to. Each delegation-completed event for a
-- budgeted tier accumulates its measured headroom drawdown
-- (budget_headroom_consumed_usd, 0018) plus any cash overage into the period
-- row; headroom_remaining_usd is derived = cap - consumed (floored at 0). The
-- write is idempotent per source event via last_correlation_id so a replayed
-- event does not double-count.
--
-- Idempotent CREATE/ADD so warm dev/stability volumes reconcile cleanly.

CREATE TABLE IF NOT EXISTS delegation_budget_state (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    cost_tier_name TEXT NOT NULL,
    budget_period TEXT NOT NULL,
    monthly_cap_usd NUMERIC(18, 6) NOT NULL CHECK (monthly_cap_usd >= 0),
    consumed_usd NUMERIC(18, 6) NOT NULL DEFAULT 0 CHECK (consumed_usd >= 0),
    overage_usd NUMERIC(18, 6) NOT NULL DEFAULT 0 CHECK (overage_usd >= 0),
    headroom_remaining_usd NUMERIC(18, 6) NOT NULL DEFAULT 0
        CHECK (headroom_remaining_usd >= 0),
    delegation_count INTEGER NOT NULL DEFAULT 0 CHECK (delegation_count >= 0),
    last_correlation_id TEXT,
    first_event_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_event_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_delegation_budget_state_identity
    ON delegation_budget_state (tenant_id, cost_tier_name, budget_period);

CREATE OR REPLACE FUNCTION refresh_delegation_budget_state_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_delegation_budget_state_updated_at
    ON delegation_budget_state;
CREATE TRIGGER trg_delegation_budget_state_updated_at
    BEFORE UPDATE ON delegation_budget_state
    FOR EACH ROW
    EXECUTE FUNCTION refresh_delegation_budget_state_updated_at();
