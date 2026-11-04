-- Creates the context-audit projection in the application database.
--
-- Flat infrastructure migration 053 creates a table in a different database;
-- it does not provision this application's projection. Fresh installations need
-- the node's own table, while existing installations retain their rows and
-- BIGSERIAL identity. The deterministic source-coordinate key and its unique
-- index make replays converge. Legacy rows keep a NULL event_key.

CREATE TABLE IF NOT EXISTS public.context_audit_events (
    id                      BIGSERIAL       PRIMARY KEY,
    task_id                 TEXT            NOT NULL,
    parent_task_id          TEXT,
    correlation_id          TEXT            NOT NULL,
    contract_id             TEXT,
    event_type              TEXT            NOT NULL,
    enforcement_level       TEXT            NOT NULL,
    enforcement_action      TEXT,
    violation_details       JSONB,
    context_tokens_used     INT,
    context_budget_tokens   INT,
    return_tokens           INT,
    return_max_tokens       INT,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

ALTER TABLE public.context_audit_events
    ADD COLUMN IF NOT EXISTS event_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_context_audit_events_event_key
    ON public.context_audit_events (event_key);

COMMENT ON COLUMN public.context_audit_events.event_key IS
    'Deterministic key of the source event: sha256 of topic:partition:offset in UUID shape. NULL on rows written before node_projection_context_audit.';
