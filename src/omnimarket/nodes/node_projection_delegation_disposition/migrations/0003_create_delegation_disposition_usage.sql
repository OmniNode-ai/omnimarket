-- OMN-20242. Owned by omnimarket.nodes.node_projection_delegation_disposition.
-- Cross-node read: delegation_events belongs to node_projection_delegation.
-- Its correlation_id is TEXT, and its tenant_id is UUID once migration 0031
-- has run (TEXT before it). Both sides are compared as TEXT so the join is
-- type-correct on either shape and never casts a legacy slug to UUID.
-- Requires node_projection_delegation migrations 0007, 0022 and 0048 first.
CREATE OR REPLACE VIEW public.delegation_disposition_usage
WITH (security_invoker = true) AS
SELECT
    e.tenant_id,
    e.model_name,
    e.task_type,
    e.caller_lane,
    COUNT(*) AS delegations_total,
    COUNT(*) FILTER (WHERE d.disposition = 'accepted_as_is') AS accepted_as_is_n,
    COUNT(*) FILTER (WHERE d.disposition = 'edited') AS edited_n,
    COUNT(*) FILTER (WHERE d.disposition = 'rejected') AS rejected_n,
    COUNT(*) FILTER (WHERE d.disposition = 'ignored') AS ignored_n,
    COUNT(*) FILTER (WHERE d.disposition_id IS NULL) AS undisposed_n
FROM public.delegation_events e
LEFT JOIN public.delegation_dispositions d
    ON d.tenant_id::text = e.tenant_id::text
    AND d.delegation_correlation_id::text = e.correlation_id::text
GROUP BY e.tenant_id, e.model_name, e.task_type, e.caller_lane;

-- Match delegation aggregate views: both grantees already read the base tables.
GRANT SELECT ON public.delegation_disposition_usage TO app_dashboard;
GRANT SELECT ON public.delegation_disposition_usage TO tenant_projection_writer;
