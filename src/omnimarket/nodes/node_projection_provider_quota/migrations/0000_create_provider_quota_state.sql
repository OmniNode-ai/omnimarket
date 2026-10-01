-- OMN-20154. Owned by omnimarket.nodes.node_projection_provider_quota.
-- Durable provider quota state: one row per (tenant, credential reference,
-- provider, model scope); model_scope '*' is the provider-wide row. Routing and
-- the judge read the active blocks from here instead of process memory.
-- credential_ref holds a reference NAME, never a secret value.
CREATE TABLE IF NOT EXISTS public.provider_quota_state (
    tenant_id UUID NOT NULL,
    credential_ref TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model_scope TEXT NOT NULL,
    calls_total BIGINT NOT NULL DEFAULT 0,
    hits_total BIGINT NOT NULL DEFAULT 0,
    window_seconds INT NOT NULL DEFAULT 60,
    window_started_at TIMESTAMPTZ,
    window_calls INT NOT NULL DEFAULT 0,
    last_call_at TIMESTAMPTZ,
    last_outcome TEXT,
    last_http_status INT,
    last_provider_code TEXT,
    last_hit_at TIMESTAMPTZ,
    disposition TEXT,
    blocked_until TIMESTAMPTZ,
    blocked_indefinitely BOOLEAN NOT NULL DEFAULT FALSE,
    block_reason TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor BIGSERIAL,
    PRIMARY KEY (tenant_id, credential_ref, provider_id, model_scope)
);
-- ---- BEGIN OMN-15376 shape reconciliation: provider_quota_state ----
-- COLUMN RECONCILIATION: one guarded ADD COLUMN per declared column, so CREATE TABLE IF NOT EXISTS stays idempotent in SHAPE, not just existence.
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS credential_ref TEXT;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS provider_id TEXT;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS model_scope TEXT;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS calls_total BIGINT;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS hits_total BIGINT;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS window_seconds INT;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS window_started_at TIMESTAMPTZ;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS window_calls INT;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS last_call_at TIMESTAMPTZ;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS last_outcome TEXT;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS last_http_status INT;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS last_provider_code TEXT;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS last_hit_at TIMESTAMPTZ;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS disposition TEXT;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS blocked_until TIMESTAMPTZ;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS blocked_indefinitely BOOLEAN;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS block_reason TEXT;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
ALTER TABLE public.provider_quota_state
    ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;
-- DEFAULT RECONCILIATION: a pre-existing table keeps the columns it has, so the
-- declared defaults are set explicitly (idempotent).
ALTER TABLE public.provider_quota_state
    ALTER COLUMN calls_total SET DEFAULT 0;
ALTER TABLE public.provider_quota_state
    ALTER COLUMN hits_total SET DEFAULT 0;
ALTER TABLE public.provider_quota_state
    ALTER COLUMN window_seconds SET DEFAULT 60;
ALTER TABLE public.provider_quota_state
    ALTER COLUMN window_calls SET DEFAULT 0;
ALTER TABLE public.provider_quota_state
    ALTER COLUMN blocked_indefinitely SET DEFAULT FALSE;
ALTER TABLE public.provider_quota_state
    ALTER COLUMN first_seen_at SET DEFAULT NOW();
ALTER TABLE public.provider_quota_state
    ALTER COLUMN updated_at SET DEFAULT NOW();
-- NOT NULL RECONCILIATION: SET NOT NULL refuses (fails the migration) when a
-- pre-existing row holds NULL; it never invents a value.
ALTER TABLE public.provider_quota_state ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE public.provider_quota_state ALTER COLUMN credential_ref SET NOT NULL;
ALTER TABLE public.provider_quota_state ALTER COLUMN provider_id SET NOT NULL;
ALTER TABLE public.provider_quota_state ALTER COLUMN model_scope SET NOT NULL;
ALTER TABLE public.provider_quota_state ALTER COLUMN calls_total SET NOT NULL;
ALTER TABLE public.provider_quota_state ALTER COLUMN hits_total SET NOT NULL;
ALTER TABLE public.provider_quota_state ALTER COLUMN window_seconds SET NOT NULL;
ALTER TABLE public.provider_quota_state ALTER COLUMN window_calls SET NOT NULL;
ALTER TABLE public.provider_quota_state ALTER COLUMN blocked_indefinitely SET NOT NULL;
ALTER TABLE public.provider_quota_state ALTER COLUMN observed_at SET NOT NULL;
ALTER TABLE public.provider_quota_state ALTER COLUMN first_seen_at SET NOT NULL;
ALTER TABLE public.provider_quota_state ALTER COLUMN updated_at SET NOT NULL;
-- PRIMARY KEY RECONCILIATION.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.provider_quota_state'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.provider_quota_state
            ADD CONSTRAINT provider_quota_state_pkey
            PRIMARY KEY (tenant_id, credential_ref, provider_id, model_scope);
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: provider_quota_state ----
-- Routing reads the ACTIVE blocks of one tenant on every decision.
CREATE INDEX IF NOT EXISTS provider_quota_state_active_block_idx
    ON public.provider_quota_state (tenant_id, blocked_until)
    WHERE blocked_until IS NOT NULL OR blocked_indefinitely;
ALTER TABLE public.provider_quota_state ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.provider_quota_state;
CREATE POLICY tenant_isolation ON public.provider_quota_state
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.provider_quota_state TO app_dashboard;
