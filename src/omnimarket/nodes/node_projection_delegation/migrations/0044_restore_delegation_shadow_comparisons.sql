-- OMN-18693 / OMN-15714: restore the delegation shadow-comparison projection.
--
-- The historical public table was deleted with the old infra migration stream.
-- Its current contract is TENANT while the physical-schema bridge keeps the
-- relation in public until OMN-15359. New rows therefore need an explicit UUID
-- tenant identity and the canonical FORCE-RLS posture. A historical table with
-- rows cannot be attributed safely: this migration fails before changing it.

BEGIN;

CREATE TABLE IF NOT EXISTS public.delegation_shadow_comparisons (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id TEXT UNIQUE NOT NULL,
    tenant_id UUID NOT NULL,
    session_id TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    task_type TEXT NOT NULL,
    primary_agent TEXT NOT NULL,
    shadow_agent TEXT NOT NULL,
    divergence_detected BOOLEAN DEFAULT false,
    divergence_score NUMERIC(18, 9),
    primary_latency_ms INT,
    shadow_latency_ms INT,
    primary_cost_usd NUMERIC(18, 9),
    shadow_cost_usd NUMERIC(18, 9),
    divergence_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $restore_delegation_shadow_comparisons$
DECLARE
    has_tenant_id BOOLEAN;
    tenant_type TEXT;
    tenant_not_null BOOLEAN;
    tenant_default TEXT;
BEGIN
    SELECT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_attribute attribute
        WHERE attribute.attrelid = 'public.delegation_shadow_comparisons'::regclass
          AND attribute.attname = 'tenant_id'
          AND NOT attribute.attisdropped
    )
    INTO has_tenant_id;

    IF NOT has_tenant_id THEN
        IF EXISTS (SELECT 1 FROM public.delegation_shadow_comparisons) THEN
            RAISE EXCEPTION
                'delegation_shadow_comparisons has legacy rows with no tenant attribution; refusing tenant backfill';
        END IF;
        ALTER TABLE public.delegation_shadow_comparisons
            ADD COLUMN tenant_id UUID;
        ALTER TABLE public.delegation_shadow_comparisons
            ALTER COLUMN tenant_id SET NOT NULL;
    ELSE
        SELECT attribute.atttypid::regtype::text, attribute.attnotnull,
               pg_catalog.pg_get_expr(default_value.adbin, default_value.adrelid)
        INTO tenant_type, tenant_not_null, tenant_default
        FROM pg_catalog.pg_attribute attribute
        LEFT JOIN pg_catalog.pg_attrdef default_value
          ON default_value.adrelid = attribute.attrelid
         AND default_value.adnum = attribute.attnum
        WHERE attribute.attrelid = 'public.delegation_shadow_comparisons'::regclass
          AND attribute.attname = 'tenant_id'
          AND NOT attribute.attisdropped;

        IF tenant_type <> 'uuid' THEN
            RAISE EXCEPTION
                'delegation_shadow_comparisons.tenant_id must be uuid, found %',
                tenant_type;
        END IF;
        IF NOT tenant_not_null THEN
            IF EXISTS (
                SELECT 1
                FROM public.delegation_shadow_comparisons
                WHERE tenant_id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'delegation_shadow_comparisons has NULL tenant_id rows; refusing tenant backfill';
            END IF;
            ALTER TABLE public.delegation_shadow_comparisons
                ALTER COLUMN tenant_id SET NOT NULL;
        END IF;
        IF tenant_default IS NOT NULL THEN
            ALTER TABLE public.delegation_shadow_comparisons
                ALTER COLUMN tenant_id DROP DEFAULT;
        END IF;
    END IF;

    ALTER TABLE public.delegation_shadow_comparisons ENABLE ROW LEVEL SECURITY;
    ALTER TABLE public.delegation_shadow_comparisons FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON public.delegation_shadow_comparisons;
    CREATE POLICY tenant_isolation ON public.delegation_shadow_comparisons
        FOR ALL
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
END
$restore_delegation_shadow_comparisons$;

CREATE INDEX IF NOT EXISTS idx_delegation_shadow_comparisons_session_id
    ON public.delegation_shadow_comparisons (session_id);
CREATE INDEX IF NOT EXISTS idx_delegation_shadow_comparisons_timestamp
    ON public.delegation_shadow_comparisons (timestamp DESC);

DO $require_tenant_projection_writer$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'tenant_projection_writer'
    ) THEN
        RAISE EXCEPTION
            'tenant_projection_writer role missing; apply flat migration 103 before node migrations';
    END IF;
END
$require_tenant_projection_writer$;

GRANT USAGE ON SCHEMA public TO tenant_projection_writer;
GRANT SELECT, INSERT, UPDATE ON public.delegation_shadow_comparisons TO tenant_projection_writer;
GRANT USAGE ON SCHEMA public TO app_dashboard;
GRANT SELECT ON public.delegation_shadow_comparisons TO app_dashboard;

COMMIT;
