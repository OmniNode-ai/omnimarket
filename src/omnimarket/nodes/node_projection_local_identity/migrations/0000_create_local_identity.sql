-- OMN-19986: the local install's tenant identity and when it was minted, one row
-- per tenant, folded from onex.evt.omnimarket.local-identity-minted.v1.
-- Owner: omnimarket.nodes.node_projection_local_identity
-- Target database: omnidash_analytics; physical schema: public.
--
-- The key is tenant_id. The row keeps the earliest minted_at any announcement of
-- that identity carried: a replay, or a later announcement, writes nothing, and an
-- earlier one moves minted_at back. The cursor is re-stamped from the sequence on
-- every write.

CREATE TABLE IF NOT EXISTS public.local_identity (
    tenant_id TEXT NOT NULL CHECK (tenant_id <> ''),
    minted_at TIMESTAMPTZ NOT NULL,
    event_id UUID NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor BIGSERIAL NOT NULL,
    PRIMARY KEY (tenant_id)
);

-- ---- BEGIN OMN-15376 shape reconciliation: local_identity ----
ALTER TABLE public.local_identity ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE public.local_identity ADD COLUMN IF NOT EXISTS minted_at TIMESTAMPTZ;
ALTER TABLE public.local_identity ADD COLUMN IF NOT EXISTS event_id UUID;
ALTER TABLE public.local_identity ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE public.local_identity ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;

-- Static NOT NULL convergence: a column already holding NULLs fails the ALTER, the
-- same outcome as a guard would give, with every relation target visible to the SQL gate.
ALTER TABLE public.local_identity ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE public.local_identity ALTER COLUMN minted_at SET NOT NULL;
ALTER TABLE public.local_identity ALTER COLUMN event_id SET NOT NULL;
ALTER TABLE public.local_identity ALTER COLUMN updated_at SET NOT NULL;
ALTER TABLE public.local_identity ALTER COLUMN projection_cursor SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_identity'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.local_identity
            ADD CONSTRAINT local_identity_pkey PRIMARY KEY (tenant_id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_identity'::regclass
          AND contype = 'c' AND conname = 'local_identity_tenant_id_check'
    ) THEN
        ALTER TABLE public.local_identity
            ADD CONSTRAINT local_identity_tenant_id_check CHECK (tenant_id <> '');
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: local_identity ----

CREATE UNIQUE INDEX IF NOT EXISTS idx_local_identity_projection_cursor
    ON public.local_identity (projection_cursor);

-- Tenant isolation from the first migration: the table is created with row-level
-- security enabled and its policy in place. FORCE is a separate, fenced step
-- (0002), so this file and the 0001 grants apply on every lane.
ALTER TABLE public.local_identity ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.local_identity;
CREATE POLICY tenant_isolation ON public.local_identity
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
GRANT SELECT ON public.local_identity TO app_dashboard;
