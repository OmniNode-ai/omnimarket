-- The tenant projection writer needs SELECT and INSERT on the table this node writes. The cluster role is provisioned
-- before node migrations run; a database without it refuses here instead of granting to nothing.
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
GRANT SELECT, INSERT ON public.agent_messages TO tenant_projection_writer;
