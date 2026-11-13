-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
--
-- OMN-24286: the consolidated tenant projection writer reads and writes the experiment arm
-- results table and advances its projection cursor sequence.

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
GRANT USAGE
    ON SEQUENCE public.experiment_arm_results_projection_cursor_seq
    TO tenant_projection_writer;

SELECT 1 / count(*) AS experiment_arm_results_projection_cursor_sequence_usage_assertion
WHERE has_sequence_privilege(
    'tenant_projection_writer',
    'public.experiment_arm_results_projection_cursor_seq',
    'USAGE'
);
