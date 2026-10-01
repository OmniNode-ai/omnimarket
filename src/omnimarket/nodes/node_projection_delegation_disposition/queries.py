# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tenant-scoped usage query run by readers under the tenant's RLS scope.

Usage is a reader query because delegation_events belongs to another node.
A view in this node's migrations fails on an empty database when its migration
runs before the owning node creates delegation_events; cross-node migration
ordering cannot safely ensure that dependency exists. Compare tenant and
correlation keys as text to support both TEXT and UUID event tenant columns.
"""

DISPOSITION_USAGE_QUERY: str = """
SELECT
    e.model_name,
    e.task_type,
    e.caller_lane,
    COUNT(*) AS delegations_total,
    COUNT(*) FILTER (WHERE d.disposition = 'accepted_as_is') AS accepted_as_is_n,
    COUNT(*) FILTER (WHERE d.disposition = 'edited') AS edited_n,
    COUNT(*) FILTER (WHERE d.disposition = 'rejected') AS rejected_n,
    COUNT(*) FILTER (WHERE d.disposition = 'ignored') AS ignored_n,
    COUNT(*) FILTER (WHERE d.disposition_id IS NULL) AS undisposed_n
FROM delegation_events e
LEFT JOIN delegation_dispositions d
    ON d.tenant_id::text = e.tenant_id::text
    AND d.delegation_correlation_id::text = e.correlation_id::text
WHERE e.tenant_id::text = $1
GROUP BY e.model_name, e.task_type, e.caller_lane;
"""
