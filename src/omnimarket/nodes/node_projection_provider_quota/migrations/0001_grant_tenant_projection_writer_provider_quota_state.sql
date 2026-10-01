-- OMN-20154: the tenant projection writer needs table and BIGSERIAL sequence grants.
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
GRANT SELECT, INSERT, UPDATE
    ON public.provider_quota_state
    TO tenant_projection_writer;
GRANT USAGE
    ON SEQUENCE public.provider_quota_state_projection_cursor_seq
    TO tenant_projection_writer;

SELECT 1 / count(*) AS provider_quota_state_projection_cursor_sequence_usage_assertion
WHERE has_sequence_privilege(
    'tenant_projection_writer',
    'public.provider_quota_state_projection_cursor_seq',
    'USAGE'
);
