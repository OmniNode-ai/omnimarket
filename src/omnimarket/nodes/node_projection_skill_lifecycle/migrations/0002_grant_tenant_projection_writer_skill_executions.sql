-- OMN-21933: the tenant projection writer needs table grants on skill_executions,
-- which node_projection_skill_lifecycle writes. Flat migration 103 provisions the
-- cluster role before node migrations run. skill_executions has no BIGSERIAL
-- column, so no sequence grant is needed.
DO $require_tenant_projection_writer$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'tenant_projection_writer'
          AND NOT rolsuper AND NOT rolbypassrls
    ) THEN
        RAISE EXCEPTION
            'tenant_projection_writer role missing or RLS-bypassing; apply flat migration 103 before node migrations';
    END IF;
END
$require_tenant_projection_writer$;

-- Skill runs are tenant workload data, as in node_projection_skill_executions.
-- The existing event stream omits tenant_id; its rows belong to the house
-- tenant. Match the sibling's TEXT slug and require the writer's transaction
-- to bind app.tenant_id. An unset GUC never falls back to the house tenant.
ALTER TABLE public.skill_executions
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'omninode';
ALTER TABLE public.skill_executions ALTER COLUMN tenant_id SET DEFAULT 'omninode';
ALTER TABLE public.skill_executions ALTER COLUMN tenant_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_skill_executions_tenant_id
    ON public.skill_executions (tenant_id);
ALTER TABLE public.skill_executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.skill_executions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.skill_executions;
CREATE POLICY tenant_isolation ON public.skill_executions
    FOR ALL
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT USAGE ON SCHEMA public TO tenant_projection_writer;
-- UPDATE is granted although the node only inserts (ON CONFLICT DO NOTHING): the
-- contract declares the table for write, and the topology derives SELECT, INSERT,
-- UPDATE from that. A narrower grant would leave the derived UPDATE undelivered.
GRANT SELECT, INSERT, UPDATE
    ON public.skill_executions
    TO tenant_projection_writer;

DO $assert_tenant_projection_writer_privileges$
DECLARE
    privilege text;
BEGIN
    FOREACH privilege IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE'] LOOP
        IF NOT has_table_privilege('tenant_projection_writer', 'public.skill_executions', privilege) THEN
            RAISE EXCEPTION
                'missing required tenant_projection_writer privilege % on public.skill_executions',
                privilege;
        END IF;
    END LOOP;
END
$assert_tenant_projection_writer_privileges$;
