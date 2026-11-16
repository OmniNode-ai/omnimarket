-- OMN-24171: the tenant projection writer needs table and BIGSERIAL sequence grants on the
-- four eval-results stores. Flat migration 103 provisions the cluster role before node
-- migrations run.
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
    ON public.eval_item_scores, public.eval_run_closes, public.eval_run_rates
    TO tenant_projection_writer;
GRANT SELECT, INSERT
    ON public.eval_item_traces
    TO tenant_projection_writer;
GRANT DELETE ON public.eval_run_rates TO tenant_projection_writer;
GRANT USAGE
    ON SEQUENCE public.eval_item_scores_projection_cursor_seq,
                public.eval_item_traces_projection_cursor_seq,
                public.eval_run_closes_projection_cursor_seq,
                public.eval_run_rates_projection_cursor_seq
    TO tenant_projection_writer;

SELECT 1 / count(*) AS eval_results_projection_cursor_sequence_usage_assertion
WHERE has_sequence_privilege(
    'tenant_projection_writer',
    'public.eval_item_scores_projection_cursor_seq',
    'USAGE'
);
