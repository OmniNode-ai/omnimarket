-- OMN-24171: identity columns use the table INSERT privilege without sequence grants.
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
    ON public.eval_item_scores, public.eval_item_traces, public.eval_run_closes, public.eval_run_rates
    TO tenant_projection_writer;
GRANT DELETE ON public.eval_item_traces, public.eval_run_rates TO tenant_projection_writer;
SELECT 1 / (CASE WHEN count(*) = 4 THEN 1 ELSE 0 END) AS eval_results_insert_grant_assertion
FROM unnest(ARRAY['eval_item_scores', 'eval_item_traces', 'eval_run_closes', 'eval_run_rates']) AS t(table_name)
WHERE has_table_privilege('tenant_projection_writer', t.table_name, 'INSERT');
