-- OMN-14058: add tenant_id to savings_estimates.
--
-- HandlerProjectionSavings.project_delegate_skill_savings() stamps
-- row["tenant_id"] whenever the source delegate-skill terminal event carries
-- a non-blank tenant identity -- the key is omitted, never written as NULL,
-- when no tenant is resolved. The column did not exist on this table:
-- PostgresSyncProjectionAdapter.upsert() builds its INSERT column list
-- directly from row.keys(), so the first write with ONEX_TENANT_ID set
-- raised `column "tenant_id" does not exist` against real Postgres. The
-- in-memory dict adapter used by unit tests has no schema to violate, so it
-- masked the gap entirely.
--
-- DEFAULT 'omninode' mirrors the interim single-tenant convention already
-- used elsewhere on this projection surface (0019_delegation_budget_state.sql's
-- DEFAULT_TENANT, and the cloud-only omnidash/db/migrations/0001_tenant_rls.sql,
-- which applies the same default to a different database via a manual,
-- operator-run apply plan and never reaches this table). Existing rows and
-- unstamped writes land under the default; a caller-supplied tenant_id
-- overrides it on INSERT.

ALTER TABLE savings_estimates
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'omninode';

CREATE INDEX IF NOT EXISTS idx_savings_estimates_tenant_id
    ON savings_estimates (tenant_id);
