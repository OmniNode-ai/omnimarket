-- Delivers the tenant_projection_writer grants for public.context_audit_events.
--
-- node_projection_context_audit declares the table read_write, so the topology
-- derives SELECT, INSERT and UPDATE for tenant_projection_writer on it. A
-- declared grant with no delivering migration is refused by the topology grant
-- delivery gate, and a live ACL seen on one lane is not a delivered grant.
--
-- Node migration 0000 creates a BIGSERIAL id, so every INSERT
-- evaluates a nextval() DEFAULT over a standalone sequence whose own ACL
-- PostgreSQL checks separately; the sequence is granted here by its static name.
-- SELECT and UPDATE are granted because the writer's statement is an
-- INSERT ... ON CONFLICT (event_key) DO UPDATE.
--
-- The application migration identity cannot create cluster roles. Provision
-- tenant_projection_writer at the cluster-role seam before applying this file.
-- GRANT is idempotent, so the file is safe to re-run.

DO $require_tenant_projection_writer$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'tenant_projection_writer'
    ) THEN
        RAISE EXCEPTION
            'tenant_projection_writer role missing; provision the cluster role before node migrations'
            USING HINT = 'Run scripts/provision-cluster-roles.sh --apply with the cluster provisioning identity, then retry.';
    END IF;
END
$require_tenant_projection_writer$;

-- Validate the actual dependency before issuing any ACL. Static targets keep
-- the application-domain SQL validator able to inspect every granted object;
-- an unexpected sequence needs an explicit schema repair, not a guessed grant.
DO $require_context_audit_sequence$
BEGIN
    IF pg_get_serial_sequence('public.context_audit_events', 'id')
        IS DISTINCT FROM 'public.context_audit_events_id_seq' THEN
        RAISE EXCEPTION
            'public.context_audit_events.id is not backed by context_audit_events_id_seq';
    END IF;
END
$require_context_audit_sequence$;

GRANT USAGE ON SCHEMA public TO tenant_projection_writer;
GRANT SELECT, INSERT, UPDATE ON public.context_audit_events TO tenant_projection_writer;
GRANT USAGE ON SEQUENCE public.context_audit_events_id_seq TO tenant_projection_writer;

DO $assert_tenant_projection_writer_privileges$
DECLARE
    privilege text;
BEGIN
    FOREACH privilege IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE'] LOOP
        IF NOT has_table_privilege(
            'tenant_projection_writer', 'public.context_audit_events', privilege
        ) THEN
            RAISE EXCEPTION
                'missing required tenant_projection_writer privilege % on public.context_audit_events',
                privilege;
        END IF;
    END LOOP;
    IF NOT has_sequence_privilege(
        'tenant_projection_writer', 'public.context_audit_events_id_seq', 'USAGE'
    ) THEN
        RAISE EXCEPTION
            'missing required tenant_projection_writer USAGE on public.context_audit_events_id_seq';
    END IF;
END
$assert_tenant_projection_writer_privileges$;
