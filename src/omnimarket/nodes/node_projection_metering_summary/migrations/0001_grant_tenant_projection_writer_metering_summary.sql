-- OMN-19977. Projection writer's exact read and write grants on public.metering_summary.
GRANT SELECT, INSERT, UPDATE ON public.metering_summary TO tenant_projection_writer;

SELECT 1 / count(*) AS metering_summary_writer_privilege_assertion
WHERE has_table_privilege('tenant_projection_writer', 'public.metering_summary', 'SELECT')
  AND has_table_privilege('tenant_projection_writer', 'public.metering_summary', 'INSERT')
  AND has_table_privilege('tenant_projection_writer', 'public.metering_summary', 'UPDATE');
