-- OMN-21920: the tenant projection writer needs table grants on the six agent_*
-- observability tables this node writes. Flat migration 103 provisions the cluster
-- role before node migrations run. None of the six has a BIGSERIAL column, so no
-- sequence grant is needed.
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
-- UPDATE is granted although five of the six tables only insert: the contract
-- declares the tables for write, and the topology derives SELECT, INSERT, UPDATE
-- from that. A narrower grant here would leave the derived UPDATE undelivered.
GRANT SELECT, INSERT, UPDATE
    ON public.agent_actions,
       public.agent_transformation_events,
       public.router_performance_metrics,
       public.agent_detection_failures,
       public.agent_execution_logs,
       public.agent_status_events
    TO tenant_projection_writer;

DO $assert_tenant_projection_writer_privileges$
DECLARE
    relation text;
    privilege text;
BEGIN
    FOREACH relation IN ARRAY ARRAY[
        'public.agent_actions',
        'public.agent_transformation_events',
        'public.router_performance_metrics',
        'public.agent_detection_failures',
        'public.agent_execution_logs',
        'public.agent_status_events'
    ] LOOP
        FOREACH privilege IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE'] LOOP
            IF NOT has_table_privilege('tenant_projection_writer', relation, privilege) THEN
                RAISE EXCEPTION
                    'missing required tenant_projection_writer privilege % on %',
                    privilege, relation;
            END IF;
        END LOOP;
    END LOOP;
END
$assert_tenant_projection_writer_privileges$;
