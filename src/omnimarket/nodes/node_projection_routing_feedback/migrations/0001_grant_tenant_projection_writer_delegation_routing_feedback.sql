-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- The platform routing-feedback writer runs as the tenant projection writer principal.
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
    ON public.delegation_routing_feedback
    TO tenant_projection_writer;

SELECT 1 / count(*) AS delegation_routing_feedback_writer_grants_assertion
FROM (
    SELECT grantee
    FROM information_schema.role_table_grants
    WHERE table_schema = 'public'
      AND table_name = 'delegation_routing_feedback'
      AND grantee = 'tenant_projection_writer'
      AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE')
    GROUP BY grantee
    HAVING count(DISTINCT privilege_type) = 3
) AS granted;
