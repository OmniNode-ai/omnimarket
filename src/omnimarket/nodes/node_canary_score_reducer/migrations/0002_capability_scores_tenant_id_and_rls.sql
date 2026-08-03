-- OMN-15655 / operator ruling 2026-08-02 (house tenant): give capability_scores a
-- tenant identity and row-level tenant isolation.
--
-- canary capability scores are per-workload model/task quality measurements THIS RELATION IS TENANT DATA
--   WHY, so the row is workload-attributable and belongs in the tenant
--   domain per the ruling's rule. Rows produced by OmniNode's own platform
--   workloads are the omninode TENANT's rows -- OmniNode is a first-class tenant,
--   not an absence of one. Only attribution-meaningless infrastructure state
--   (migration bookkeeping, registry, orchestration state, deployment
--   evidence) stays in omninode_internal.
--
--   This migration lands alongside the contract flip
--   `db_io.db_tables[].schema: public -> tenant`. Before it, the relation
--   declared a schema the typed topology does not declare, which ADR-0027
--   refuses by design -- `ValueError: Unknown schema 'public' for
--   database_ref 'application'` out of `ModelDeploymentTopology.schema_domain`.
--
-- IDENTITY TYPE -- TEXT, NOT UUID, AND THAT IS DELIBERATE
--   Every landed tenant_id column on this surface is `TEXT` holding the slug
--   'omninode' (delegation 0022/0025, savings 080, registration 0002,
--   inference-response 0002, omnidash 0001_tenant_rls), and the RLS policies
--   that already shipped (0023, 0026) compare TEXT with no `::uuid` cast.
--   OMN-15356 converts that whole set to the canonical UUID in ONE pass.
--   Adding a UUID column here alone would fork tenant identity inside a single
--   database -- the exact "one canonical model per shape" violation. The
--   canonical UUID this slug resolves to is pinned in
--   `omnimarket.projection.tenant_isolation.omninode_TENANT_UUID`.
--
-- BACKFILL -- NO BATCHING NEEDED, AND BATCHING WOULD BE WORSE
--   `ADD COLUMN ... NOT NULL DEFAULT` with a non-volatile default does not
--   rewrite the table on PostgreSQL 11+; the default is stored in
--   `pg_attribute.attmissingval` and every pre-existing row reads back as
--   the house tenant immediately. A batched `UPDATE` would do strictly more
--   work (a real rewrite, dead tuples, a longer lock) for the same end state.
--
-- PHYSICAL SCHEMA IS NOT MOVED HERE -- SEE OMN-15359
--   No `ALTER TABLE ... SET SCHEMA tenant`. The `tenant` schema is created
--   by no applied migration in this repo or in omnibase_infra
--   (`docker/migrations/forward/_ledger/bootstrap.sql` creates only
--   `platform_catalog`); it exists solely in proof fixtures. Every relation
--   already classified TENANT -- delegation_events, delegation_budget_state,
--   savings_estimates -- is still physically in `public`
--   (`current_schema: ["public"]` in the OMN-15423 inventory, against
--   `target_schema: tenant`). Relocating these eight alone would create the
--   split the inventory's target/current split exists to prevent, and would
--   fail outright against a database with no `tenant` schema. The physical
--   move is OMN-15359 ("Build classified schemas and migrate internal,
--   control-plane, catalog, and tenant targets"), which moves the whole set,
--   with its trigger functions and sequences, in one governed cutover.
--
-- FAIL-CLOSED RATCHET
--   The column DEFAULT is what supplies the house tenant today, exactly as it
--   does for savings_estimates (OMN-14058, operator-accepted): a writer that
--   resolves no tenant OMITS the key and Postgres fills it -- the key is never
--   written as NULL. That default-allowed state is PINNED, with its flip
--   condition named, by
--   `tests/unit/projection/test_house_tenant_default_ratchet.py`. When
--   customer ingress exists the writer boundary stops defaulting and refuses
--   instead; that test fails the moment the precondition changes.
--
-- BLAST RADIUS -- FORCE constrains the table OWNER too
--   Same caveat as migration 0023: a writer connected as the `postgres`
--   SUPERUSER (the compose lanes) bypasses RLS regardless of FORCE. The real
--   isolation boundary is this policy PLUS a non-superuser, NOBYPASSRLS writer
--   role (OMN-14899 / OMN-15425).
--
-- Fail-closed: `current_setting('app.tenant_id', true)` is NULL when the GUC
-- is unset, the predicate is NULL, and zero rows are visible. The policy has no
-- default-tenant fallback, by design.
--
-- Idempotent: ADD COLUMN / CREATE INDEX are IF NOT EXISTS, ENABLE/FORCE are
-- idempotent, the policy is DROP + CREATE, GRANTs are idempotent.

-- Legacy upgrade guard: this tenant/RLS migration may be the first
-- capability_scores migration a legacy application DB applies. Keep the base
-- table shape convergent here instead of assuming the preceding node-owned
-- create migration has already run in every historical ledger shape.
CREATE TABLE IF NOT EXISTS public.capability_scores (
    id                      BIGSERIAL        PRIMARY KEY,
    model_key               TEXT             NOT NULL,
    task_type               TEXT             NOT NULL,
    success_count           INT              NOT NULL DEFAULT 0,
    failure_count           INT              NOT NULL DEFAULT 0,
    total_count             INT              NOT NULL DEFAULT 0,
    success_rate            DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    avg_latency_ms          INT              NOT NULL DEFAULT 0,
    avg_tokens_per_sec      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    total_cost              DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    graduated               BOOLEAN          NOT NULL DEFAULT FALSE,
    last_updated            TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    UNIQUE (model_key, task_type)
);

ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS model_key TEXT;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS task_type TEXT;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS success_count INT DEFAULT 0;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS failure_count INT DEFAULT 0;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS total_count INT DEFAULT 0;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS success_rate DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS avg_latency_ms INT DEFAULT 0;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS avg_tokens_per_sec DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS total_cost DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS graduated BOOLEAN DEFAULT FALSE;
ALTER TABLE public.capability_scores ADD COLUMN IF NOT EXISTS last_updated TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col TEXT;
    v_nulls BIGINT;
    v_pk_columns TEXT[];
BEGIN
    FOREACH v_col IN ARRAY ARRAY[
        'id', 'model_key', 'task_type', 'success_count', 'failure_count',
        'total_count', 'success_rate', 'avg_latency_ms',
        'avg_tokens_per_sec', 'total_cost', 'graduated', 'last_updated'
    ]
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL',
            'public.capability_scores'::regclass,
            v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL',
                'public.capability_scores'::regclass,
                v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15655: cannot converge public.capability_scores.% to NOT NULL -- % pre-existing row(s) hold NULL; operator data mapping required.',
                v_col, v_nulls;
        END IF;
    END LOOP;

    SELECT array_agg(a.attname::text ORDER BY k.ordinality) INTO v_pk_columns
    FROM pg_constraint c
    CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ordinality)
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
    WHERE c.conrelid = 'public.capability_scores'::regclass
      AND c.contype = 'p';

    IF v_pk_columns IS NULL THEN
        ALTER TABLE public.capability_scores
            ADD CONSTRAINT capability_scores_pkey PRIMARY KEY (id);
    ELSIF v_pk_columns <> ARRAY['id']::text[] THEN
        RAISE EXCEPTION
            'OMN-15655: public.capability_scores primary key covers %, expected {id}; operator schema ruling required.',
            v_pk_columns;
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'public.capability_scores'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY k.ordinality)
              FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ordinality)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['model_key', 'task_type']::text[]
    ) THEN
        ALTER TABLE public.capability_scores
            ADD CONSTRAINT capability_scores_model_key_task_type_key
            UNIQUE (model_key, task_type);
    END IF;
END$$;

ALTER TABLE public.capability_scores
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'omninode';

CREATE INDEX IF NOT EXISTS idx_capability_scores_tenant_id
    ON public.capability_scores (tenant_id);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = 'app_dashboard'
      AND NOT rolsuper
      AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION
      'app_dashboard role missing or RLS-bypassing - apply omnibase_infra forward migration '
      '094_create_app_dashboard_role.sql (OMN-14899) before this RLS '
      'migration. RLS grants without the constrained read role are the '
      'exact bypass this work exists to prevent.';
  END IF;
END;
$$;

GRANT USAGE ON SCHEMA public TO app_dashboard;

ALTER TABLE public.capability_scores ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.capability_scores FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON public.capability_scores;
CREATE POLICY tenant_isolation ON public.capability_scores
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- Read grant for the live dashboard reader. RLS still filters every
-- row this role can see -- the grant is what makes the RLS-scoped read path
-- reachable at all, not a widening of it. OMN-14894: sibling grant present on
-- context_roi_scores/instruction_eval_aggregate_snapshots/skill_execution_snapshots
-- in the same PR; capability_scores omitted it in error.
GRANT SELECT ON public.capability_scores TO app_dashboard;
