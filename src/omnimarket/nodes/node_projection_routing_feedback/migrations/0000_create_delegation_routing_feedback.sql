-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- Platform data, owned by omnimarket.nodes.node_projection_routing_feedback.
CREATE TABLE IF NOT EXISTS public.delegation_routing_feedback (
    model_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    success_count INTEGER NOT NULL,
    failure_count INTEGER NOT NULL,
    escalation_count INTEGER NOT NULL,
    total_count INTEGER NOT NULL,
    success_rate DOUBLE PRECISION NOT NULL,
    escalation_rate DOUBLE PRECISION NOT NULL,
    avg_latency_ms DOUBLE PRECISION NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    last_updated TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (model_id, task_type)
);
GRANT SELECT ON public.delegation_routing_feedback TO app_dashboard;
