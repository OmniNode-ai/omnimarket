-- OMN-19991: the tenant projection writer needs table and BIGSERIAL sequence grants.
-- Flat migration 103 provisions the cluster role before node migrations run.
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
-- UPDATE is needed: a newer transition for a key rewrites its one row (a raise
-- after a clear, or the clear of a raised alert). DELETE is not granted.
GRANT SELECT, INSERT, UPDATE
    ON public.local_alerts
    TO tenant_projection_writer;
-- USAGE lets the writer take nextval() for the cursor of a row it rewrites.
GRANT USAGE
    ON SEQUENCE public.local_alerts_projection_cursor_seq
    TO tenant_projection_writer;

SELECT 1 / count(*) AS local_alerts_projection_cursor_sequence_usage_assertion
WHERE has_sequence_privilege(
    'tenant_projection_writer',
    'public.local_alerts_projection_cursor_seq',
    'USAGE'
);
