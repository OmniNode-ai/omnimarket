-- The six agent_* observability tables, as omnibase_infra's forward migrations
-- 020, 022 to 025 and 027 (with 028's added columns) create them.
-- Every statement is idempotent: on a database those migrations already built,
-- this file changes nothing. Every relation is schema-qualified, as the
-- application database SQL gate requires. It exists so the node's golden chain can build the
-- schema it writes to on a database that has none.

CREATE TABLE IF NOT EXISTS public.agent_actions (
    id UUID PRIMARY KEY,
    correlation_id UUID,
    agent_name VARCHAR(255),
    action_type VARCHAR(50),
    action_name VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status VARCHAR(50),
    duration_ms INTEGER,
    result TEXT,
    error_message TEXT,
    metadata JSONB,
    project_path TEXT,
    project_name VARCHAR(255),
    working_directory TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_actions_created_at ON public.agent_actions (created_at);

CREATE TABLE IF NOT EXISTS public.agent_transformation_events (
    id UUID PRIMARY KEY,
    correlation_id UUID,
    source_agent VARCHAR(255),
    target_agent VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trigger TEXT,
    context TEXT,
    metadata JSONB,
    project_path TEXT,
    project_name VARCHAR(255),
    claude_session_id VARCHAR(255)
);
CREATE INDEX IF NOT EXISTS idx_agent_transformation_events_created_at
    ON public.agent_transformation_events (created_at);

CREATE TABLE IF NOT EXISTS public.router_performance_metrics (
    id UUID PRIMARY KEY,
    metric_name VARCHAR(255) NOT NULL,
    metric_value DECIMAL(15,6) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    correlation_id UUID,
    unit VARCHAR(50),
    agent_name VARCHAR(255),
    labels JSONB,
    metadata JSONB
);
CREATE INDEX IF NOT EXISTS idx_router_performance_metrics_created_at
    ON public.router_performance_metrics (created_at);

CREATE TABLE IF NOT EXISTS public.agent_detection_failures (
    correlation_id UUID PRIMARY KEY,
    failure_reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    request_summary TEXT,
    attempted_patterns JSONB,
    fallback_used VARCHAR(255),
    error_code VARCHAR(100),
    metadata JSONB,
    project_path TEXT,
    project_name VARCHAR(255),
    claude_session_id VARCHAR(255)
);
CREATE INDEX IF NOT EXISTS idx_agent_detection_failures_created_at
    ON public.agent_detection_failures (created_at);

CREATE TABLE IF NOT EXISTS public.agent_execution_logs (
    execution_id UUID PRIMARY KEY,
    correlation_id UUID,
    agent_name VARCHAR(255),
    status VARCHAR(50),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,
    exit_code INTEGER,
    error_message TEXT,
    input_summary TEXT,
    output_summary TEXT,
    metadata JSONB,
    project_path TEXT,
    project_name VARCHAR(255),
    claude_session_id VARCHAR(255),
    terminal_id VARCHAR(255)
);
CREATE INDEX IF NOT EXISTS idx_agent_execution_logs_updated_at
    ON public.agent_execution_logs (updated_at);

CREATE TABLE IF NOT EXISTS public.agent_status_events (
    id UUID PRIMARY KEY,
    correlation_id UUID NOT NULL,
    agent_name VARCHAR(255) NOT NULL,
    session_id VARCHAR(255) NOT NULL,
    state VARCHAR(50) NOT NULL,
    status_schema_version INTEGER NOT NULL DEFAULT 1,
    message TEXT NOT NULL,
    progress DECIMAL(3, 2),
    current_phase VARCHAR(255),
    current_task TEXT,
    blocking_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB
);
CREATE INDEX IF NOT EXISTS idx_agent_status_correlation ON public.agent_status_events (correlation_id);
CREATE INDEX IF NOT EXISTS idx_agent_status_created ON public.agent_status_events (created_at DESC);
