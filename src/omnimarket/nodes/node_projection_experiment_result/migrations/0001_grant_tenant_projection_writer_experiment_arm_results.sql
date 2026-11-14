-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
--
-- OMN-24286: the consolidated tenant projection writer reads and writes the experiment arm
-- results table. projection_cursor is an IDENTITY column, so its sequence rides the table
-- INSERT privilege and needs no separate sequence grant or ownership declaration.

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
    ON public.experiment_arm_results
    TO tenant_projection_writer;

SELECT 1 / count(*) AS experiment_arm_results_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'public'
  AND table_name = 'experiment_arm_results'
  AND grantee = 'tenant_projection_writer'
  AND privilege_type = 'INSERT';
