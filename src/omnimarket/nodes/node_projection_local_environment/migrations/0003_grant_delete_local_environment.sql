-- OMN-22030: the tenant projection writer may remove the row of a service its lane retired.
-- The observer reports a service its overlay lists as retired, and the writer deletes that service's
-- row (and publishes a tombstone). Until now the writer held SELECT, INSERT and UPDATE only, so a
-- retired report would have been refused with a permission error. Row-level security still binds the
-- delete to the tenant the transaction names: the tenant_isolation policy is FOR ALL.
-- Idempotent: a grant already held is not an error.
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

GRANT DELETE
    ON public.local_environment
    TO tenant_projection_writer;

SELECT 1 / count(*) AS local_environment_writer_delete_assertion
WHERE has_table_privilege('tenant_projection_writer', 'public.local_environment', 'DELETE');
