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

-- ---- BEGIN OMN-15376 shape reconciliation: delegation_routing_feedback ----
-- Guarded adds converge drifted tables without inventing required values.
ALTER TABLE public.delegation_routing_feedback ADD COLUMN IF NOT EXISTS model_id TEXT;
ALTER TABLE public.delegation_routing_feedback ADD COLUMN IF NOT EXISTS task_type TEXT;
ALTER TABLE public.delegation_routing_feedback ADD COLUMN IF NOT EXISTS success_count INTEGER;
ALTER TABLE public.delegation_routing_feedback ADD COLUMN IF NOT EXISTS failure_count INTEGER;
ALTER TABLE public.delegation_routing_feedback ADD COLUMN IF NOT EXISTS escalation_count INTEGER;
ALTER TABLE public.delegation_routing_feedback ADD COLUMN IF NOT EXISTS total_count INTEGER;
ALTER TABLE public.delegation_routing_feedback ADD COLUMN IF NOT EXISTS success_rate DOUBLE PRECISION;
ALTER TABLE public.delegation_routing_feedback ADD COLUMN IF NOT EXISTS escalation_rate DOUBLE PRECISION;
ALTER TABLE public.delegation_routing_feedback ADD COLUMN IF NOT EXISTS avg_latency_ms DOUBLE PRECISION;
ALTER TABLE public.delegation_routing_feedback ADD COLUMN IF NOT EXISTS window_start TIMESTAMPTZ;
ALTER TABLE public.delegation_routing_feedback ADD COLUMN IF NOT EXISTS last_updated TIMESTAMPTZ;

ALTER TABLE public.delegation_routing_feedback ALTER COLUMN model_id SET NOT NULL;
ALTER TABLE public.delegation_routing_feedback ALTER COLUMN task_type SET NOT NULL;
ALTER TABLE public.delegation_routing_feedback ALTER COLUMN success_count SET NOT NULL;
ALTER TABLE public.delegation_routing_feedback ALTER COLUMN failure_count SET NOT NULL;
ALTER TABLE public.delegation_routing_feedback ALTER COLUMN escalation_count SET NOT NULL;
ALTER TABLE public.delegation_routing_feedback ALTER COLUMN total_count SET NOT NULL;
ALTER TABLE public.delegation_routing_feedback ALTER COLUMN success_rate SET NOT NULL;
ALTER TABLE public.delegation_routing_feedback ALTER COLUMN escalation_rate SET NOT NULL;
ALTER TABLE public.delegation_routing_feedback ALTER COLUMN avg_latency_ms SET NOT NULL;
ALTER TABLE public.delegation_routing_feedback ALTER COLUMN window_start SET NOT NULL;
ALTER TABLE public.delegation_routing_feedback ALTER COLUMN last_updated SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.delegation_routing_feedback'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.delegation_routing_feedback
            ADD CONSTRAINT delegation_routing_feedback_pkey
            PRIMARY KEY (model_id, task_type);
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: delegation_routing_feedback ----

GRANT SELECT ON public.delegation_routing_feedback TO app_dashboard;
