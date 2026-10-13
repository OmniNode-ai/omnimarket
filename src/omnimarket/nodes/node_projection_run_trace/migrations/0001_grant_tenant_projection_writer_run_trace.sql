-- OMN-19987: the tenant projection writer needs table and BIGSERIAL sequence grants.
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
-- UPDATE is granted although the writer only inserts: the contract declares the
-- table read_write, and the topology derives SELECT, INSERT, UPDATE from that. A
-- narrower grant here would leave the derived UPDATE undelivered.
GRANT SELECT, INSERT, UPDATE
    ON public.run_trace
    TO tenant_projection_writer;
GRANT USAGE
    ON SEQUENCE public.run_trace_projection_cursor_seq
    TO tenant_projection_writer;

SELECT 1 / count(*) AS run_trace_projection_cursor_sequence_usage_assertion
WHERE has_sequence_privilege(
    'tenant_projection_writer',
    'public.run_trace_projection_cursor_seq',
    'USAGE'
);
