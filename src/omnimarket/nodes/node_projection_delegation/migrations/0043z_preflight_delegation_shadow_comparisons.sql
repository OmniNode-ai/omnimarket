-- OMN-18987: append-only lexical predecessor to immutable 0044.
-- Validate all legacy facts before repair DDL. Never record 0044 here.
BEGIN;

DO $omn18987_preflight$
DECLARE
    v_rows BIGINT;
    v_name TEXT;
    v_expected TEXT;
    v_type TEXT;
    v_attnum SMALLINT;
    v_has_0044 BOOLEAN := FALSE;
    v_has_primary BOOLEAN;
    v_primary_is_id BOOLEAN;
    v_has_correlation_unique BOOLEAN;
    v_has_null BOOLEAN;
    v_has_duplicates BOOLEAN;
BEGIN
    IF to_regclass('public.delegation_shadow_comparisons') IS NULL THEN
        RAISE NOTICE 'OMN-18987: delegation_shadow_comparisons absent; 0044 owns creation';
        RETURN;
    END IF;

    -- The final forward runner owns this exact ledger identity. The local
    -- projection runner is not an authority for deployed migration history.
    IF to_regclass('platform_catalog.schema_migrations') IS NOT NULL THEN
        SELECT EXISTS (
            SELECT 1 FROM platform_catalog.schema_migrations
            WHERE migration_stream = 'node:node_projection_delegation'
              AND domain = 'tenant'
              AND version = 'node:node_projection_delegation:0044_restore_delegation_shadow_comparisons.sql'
              AND checksum = '3a1089294056fafeebbe5fdbe1c0910d3dc178b37d9402e2f37c19a32161c298'
        ) INTO v_has_0044;
    END IF;

    SELECT count(*) INTO v_rows FROM public.delegation_shadow_comparisons;

    -- Every populated required fact must already be real and typed. Missing
    -- fields are repairable only on an empty relation; no historical values are invented.
    FOR v_name, v_expected IN
        SELECT * FROM (VALUES
            ('id', 'uuid'), ('correlation_id', 'text'), ('tenant_id', 'uuid'),
            ('timestamp', 'timestamp with time zone'), ('task_type', 'text'),
            ('primary_agent', 'text'), ('shadow_agent', 'text'),
            ('created_at', 'timestamp with time zone')
        ) AS required(name, expected_type)
    LOOP
        SELECT attribute.atttypid::regtype::text, attribute.attnum
          INTO v_type, v_attnum
          FROM pg_catalog.pg_attribute attribute
         WHERE attribute.attrelid = 'public.delegation_shadow_comparisons'::regclass
           AND attribute.attname = v_name AND NOT attribute.attisdropped;
        IF v_type IS NULL THEN
            IF v_rows > 0 THEN
                RAISE EXCEPTION 'OMN-18987: populated delegation_shadow_comparisons missing required column %', v_name;
            END IF;
        ELSIF v_type <> v_expected THEN
            RAISE EXCEPTION 'OMN-18987: delegation_shadow_comparisons.% must be %, found %', v_name, v_expected, v_type;
        ELSIF v_rows > 0 THEN
            EXECUTE format(
                'SELECT EXISTS (SELECT 1 FROM public.delegation_shadow_comparisons WHERE %I IS NULL)',
                v_name
            ) INTO v_has_null;
            IF v_has_null THEN
                RAISE EXCEPTION 'OMN-18987: populated delegation_shadow_comparisons has NULL required %', v_name;
            END IF;
        END IF;
    END LOOP;

    -- Optional legacy fields may be absent, but an existing incompatible type is unsafe.
    FOR v_name, v_expected IN
        SELECT * FROM (VALUES
            ('session_id', 'text'), ('divergence_detected', 'boolean'),
            ('divergence_score', 'numeric'), ('primary_latency_ms', 'integer'),
            ('shadow_latency_ms', 'integer'), ('primary_cost_usd', 'numeric'),
            ('shadow_cost_usd', 'numeric'), ('divergence_reason', 'text')
        ) AS optional(name, expected_type)
    LOOP
        SELECT attribute.atttypid::regtype::text INTO v_type
          FROM pg_catalog.pg_attribute attribute
         WHERE attribute.attrelid = 'public.delegation_shadow_comparisons'::regclass
           AND attribute.attname = v_name AND NOT attribute.attisdropped;
        IF v_type IS NOT NULL AND v_type <> v_expected
           AND NOT (v_expected = 'numeric' AND v_type LIKE 'numeric%') THEN
            RAISE EXCEPTION 'OMN-18987: optional column % must be %, found %', v_name, v_expected, v_type;
        END IF;
    END LOOP;

    IF v_rows > 0 AND EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute
        WHERE attrelid = 'public.delegation_shadow_comparisons'::regclass
          AND attname = 'id' AND NOT attisdropped
    ) THEN
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.delegation_shadow_comparisons GROUP BY id HAVING count(*) > 1)'
            INTO v_has_duplicates;
        IF v_has_duplicates THEN
            RAISE EXCEPTION 'OMN-18987: duplicate historical id values refuse primary-key repair';
        END IF;
    END IF;
    IF v_rows > 0 AND EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute
        WHERE attrelid = 'public.delegation_shadow_comparisons'::regclass
          AND attname = 'correlation_id' AND NOT attisdropped
    ) THEN
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.delegation_shadow_comparisons GROUP BY correlation_id HAVING count(*) > 1)'
            INTO v_has_duplicates;
        IF v_has_duplicates THEN
            RAISE EXCEPTION 'OMN-18987: duplicate historical correlation_id values refuse unique repair';
        END IF;
    END IF;

    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.delegation_shadow_comparisons'::regclass
          AND contype = 'p'
    ) INTO v_has_primary;
    IF v_has_primary THEN
        SELECT EXISTS (
            SELECT 1 FROM pg_catalog.pg_constraint
            WHERE conrelid = 'public.delegation_shadow_comparisons'::regclass
              AND contype = 'p'
              AND conkey = ARRAY[(SELECT attnum FROM pg_catalog.pg_attribute
                WHERE attrelid = 'public.delegation_shadow_comparisons'::regclass
                  AND attname = 'id' AND NOT attisdropped)]
        ) INTO v_primary_is_id;
        IF NOT v_primary_is_id THEN
            RAISE EXCEPTION 'OMN-18987: existing primary key is not delegation_shadow_comparisons.id; refusing rewrite';
        END IF;
    END IF;

    ALTER TABLE public.delegation_shadow_comparisons
        ADD COLUMN IF NOT EXISTS id UUID,
        ADD COLUMN IF NOT EXISTS correlation_id TEXT,
        ADD COLUMN IF NOT EXISTS tenant_id UUID,
        ADD COLUMN IF NOT EXISTS session_id TEXT,
        ADD COLUMN IF NOT EXISTS timestamp TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS task_type TEXT,
        ADD COLUMN IF NOT EXISTS primary_agent TEXT,
        ADD COLUMN IF NOT EXISTS shadow_agent TEXT,
        ADD COLUMN IF NOT EXISTS divergence_detected BOOLEAN,
        ADD COLUMN IF NOT EXISTS divergence_score NUMERIC(18, 9),
        ADD COLUMN IF NOT EXISTS primary_latency_ms INT,
        ADD COLUMN IF NOT EXISTS shadow_latency_ms INT,
        ADD COLUMN IF NOT EXISTS primary_cost_usd NUMERIC(18, 9),
        ADD COLUMN IF NOT EXISTS shadow_cost_usd NUMERIC(18, 9),
        ADD COLUMN IF NOT EXISTS divergence_reason TEXT,
        ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;

    ALTER TABLE public.delegation_shadow_comparisons
        ALTER COLUMN id SET DEFAULT gen_random_uuid(),
        ALTER COLUMN timestamp SET DEFAULT now(),
        ALTER COLUMN created_at SET DEFAULT now(),
        ALTER COLUMN id SET NOT NULL,
        ALTER COLUMN correlation_id SET NOT NULL,
        ALTER COLUMN tenant_id SET NOT NULL,
        ALTER COLUMN timestamp SET NOT NULL,
        ALTER COLUMN task_type SET NOT NULL,
        ALTER COLUMN primary_agent SET NOT NULL,
        ALTER COLUMN shadow_agent SET NOT NULL,
        ALTER COLUMN created_at SET NOT NULL,
        ALTER COLUMN tenant_id DROP DEFAULT;

    SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = 'public.delegation_shadow_comparisons'::regclass AND contype = 'p') INTO v_has_primary;
    IF NOT v_has_primary THEN
        ALTER TABLE public.delegation_shadow_comparisons ADD PRIMARY KEY (id);
    END IF;
    SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = 'public.delegation_shadow_comparisons'::regclass AND contype = 'u' AND conkey = ARRAY[(SELECT attnum FROM pg_catalog.pg_attribute WHERE attrelid = 'public.delegation_shadow_comparisons'::regclass AND attname = 'correlation_id' AND NOT attisdropped)]) INTO v_has_correlation_unique;
    IF NOT v_has_correlation_unique THEN
        ALTER TABLE public.delegation_shadow_comparisons ADD UNIQUE (correlation_id);
    END IF;

    IF v_has_0044 THEN
        ALTER TABLE public.delegation_shadow_comparisons ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.delegation_shadow_comparisons FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_isolation ON public.delegation_shadow_comparisons;
        CREATE POLICY tenant_isolation ON public.delegation_shadow_comparisons FOR ALL USING (tenant_id = current_setting('app.tenant_id', true)::uuid) WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        CREATE INDEX IF NOT EXISTS idx_delegation_shadow_comparisons_session_id ON public.delegation_shadow_comparisons (session_id);
        CREATE INDEX IF NOT EXISTS idx_delegation_shadow_comparisons_timestamp ON public.delegation_shadow_comparisons (timestamp DESC);
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'tenant_projection_writer') THEN
            RAISE EXCEPTION 'OMN-18987: tenant_projection_writer role missing';
        END IF;
        GRANT USAGE ON SCHEMA public TO tenant_projection_writer;
        GRANT SELECT, INSERT, UPDATE ON public.delegation_shadow_comparisons TO tenant_projection_writer;
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'app_dashboard') THEN
            RAISE EXCEPTION 'OMN-18987: app_dashboard role missing; apply omnibase_infra forward migration 094_create_app_dashboard_role.sql (OMN-14899) before node migrations';
        END IF;
        GRANT USAGE ON SCHEMA public TO app_dashboard;
        GRANT SELECT ON public.delegation_shadow_comparisons TO app_dashboard;
    END IF;
END
$omn18987_preflight$;

COMMIT;
