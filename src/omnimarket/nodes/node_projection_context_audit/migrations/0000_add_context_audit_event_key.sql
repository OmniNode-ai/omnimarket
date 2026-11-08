-- Adds the deterministic row key node_projection_context_audit upserts on.
--
-- public.context_audit_events is created by omnibase_infra migration 053 with a
-- BIGSERIAL id and no natural key, so the retired consumer module could only
-- INSERT and a replay wrote every event again. This node derives event_key from
-- the event's Kafka coordinates (topic, partition, offset); the writer's
-- INSERT ... ON CONFLICT (event_key) DO UPDATE then keeps one row per event.
--
-- Rows written by the retired module keep event_key NULL. A unique index treats
-- NULLs as distinct, so they stay as they are.
--
-- The application database does not carry omnibase_infra's flat migration 053
-- (that set belongs to the separate omnibase_infra service database), so the
-- table is created here with the same shape when absent. Where it already
-- exists the CREATE is a no-op. The file is safe to re-run.

-- DDL is intentionally non-concurrent: this file also runs in the verifier's
-- rollback transaction. Stop the legacy writer before deployment. Refuse a busy
-- table within five seconds and cap each statement (including index builds) at
-- one minute. The migration runner applies this file in its own psql session.
SET lock_timeout = '5s';
SET statement_timeout = '60s';

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

-- ---- BEGIN OMN-15376 shape reconciliation: context_audit_events ----
-- A pre-existing drifted table is converged onto the shape declared above with
-- guarded adds, then NOT NULL and the primary key are restored. Existing id
-- storage/defaults and event_key storage are validated, not rewritten: an
-- incompatible column needs an explicit repair. No DROP, no recreate: rows are
-- preserved, and a column that cannot be made NOT NULL fails loud.

ALTER TABLE public.context_audit_events ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE public.context_audit_events ADD COLUMN IF NOT EXISTS task_id TEXT;
ALTER TABLE public.context_audit_events ADD COLUMN IF NOT EXISTS parent_task_id TEXT;
ALTER TABLE public.context_audit_events ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE public.context_audit_events ADD COLUMN IF NOT EXISTS contract_id TEXT;
ALTER TABLE public.context_audit_events ADD COLUMN IF NOT EXISTS event_type TEXT;
ALTER TABLE public.context_audit_events ADD COLUMN IF NOT EXISTS enforcement_level TEXT;
ALTER TABLE public.context_audit_events ADD COLUMN IF NOT EXISTS enforcement_action TEXT;
ALTER TABLE public.context_audit_events ADD COLUMN IF NOT EXISTS violation_details JSONB;
ALTER TABLE public.context_audit_events ADD COLUMN IF NOT EXISTS context_tokens_used INT;
ALTER TABLE public.context_audit_events ADD COLUMN IF NOT EXISTS context_budget_tokens INT;
ALTER TABLE public.context_audit_events ADD COLUMN IF NOT EXISTS return_tokens INT;
ALTER TABLE public.context_audit_events ADD COLUMN IF NOT EXISTS return_max_tokens INT;
ALTER TABLE public.context_audit_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

-- BIGSERIAL's owned sequence alone does not prove that omitted ids are
-- generated: DROP/SET DEFAULT can leave that dependency intact. Accept a
-- BIGINT identity or an exact nextval default over the actual owned sequence.
DO $require_context_audit_id$
DECLARE
    id_type oid;
    identity_kind text;
    default_expression text;
    owned_sequence text;
BEGIN
    SELECT attribute.atttypid, attribute.attidentity,
           pg_get_expr(default_row.adbin, default_row.adrelid)
      INTO id_type, identity_kind, default_expression
      FROM pg_catalog.pg_attribute attribute
      LEFT JOIN pg_catalog.pg_attrdef default_row
        ON default_row.adrelid = attribute.attrelid
       AND default_row.adnum = attribute.attnum
     WHERE attribute.attrelid = 'public.context_audit_events'::regclass
       AND attribute.attname = 'id' AND NOT attribute.attisdropped;
    IF id_type IS DISTINCT FROM 'pg_catalog.int8'::regtype::oid THEN
        RAISE EXCEPTION 'public.context_audit_events.id must be BIGINT'
            USING HINT = 'Repair the existing id type explicitly, then retry.';
    END IF;
    owned_sequence := pg_get_serial_sequence('public.context_audit_events', 'id');
    IF owned_sequence IS NULL OR (
        identity_kind = '' AND default_expression IS DISTINCT FROM
            format('nextval(%L::regclass)', owned_sequence::regclass::text)
    ) THEN
        RAISE EXCEPTION
            'public.context_audit_events.id must generate values from its owned sequence'
            USING HINT = 'Repair the existing id default and sequence explicitly, then retry.';
    END IF;
END
$require_context_audit_id$;

-- SET NOT NULL fails loud (PostgreSQL names the column) when a pre-existing row
-- holds NULL; that needs a data ruling, so the migration refuses to guess.
ALTER TABLE public.context_audit_events ALTER COLUMN id SET NOT NULL;
ALTER TABLE public.context_audit_events ALTER COLUMN task_id SET NOT NULL;
ALTER TABLE public.context_audit_events ALTER COLUMN correlation_id SET NOT NULL;
ALTER TABLE public.context_audit_events ALTER COLUMN event_type SET NOT NULL;
ALTER TABLE public.context_audit_events ALTER COLUMN enforcement_level SET NOT NULL;
ALTER TABLE public.context_audit_events ALTER COLUMN created_at SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.context_audit_events'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.context_audit_events
            ADD CONSTRAINT context_audit_events_pkey PRIMARY KEY (id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: context_audit_events ----

CREATE INDEX IF NOT EXISTS idx_context_audit_correlation
    ON public.context_audit_events (correlation_id);
CREATE INDEX IF NOT EXISTS idx_context_audit_task
    ON public.context_audit_events (task_id);
CREATE INDEX IF NOT EXISTS idx_context_audit_type
    ON public.context_audit_events (event_type);
CREATE INDEX IF NOT EXISTS idx_context_audit_created
    ON public.context_audit_events (created_at);

ALTER TABLE public.context_audit_events
    ADD COLUMN IF NOT EXISTS event_key TEXT;

-- Refuse incompatible restored data before building the unique index. NULL
-- legacy keys remain distinct; do not delete rows or guess replacement keys.
DO $require_context_audit_event_keys$
BEGIN
    IF (SELECT atttypid FROM pg_catalog.pg_attribute
        WHERE attrelid = 'public.context_audit_events'::regclass
          AND attname = 'event_key' AND NOT attisdropped)
        IS DISTINCT FROM 'pg_catalog.text'::regtype::oid THEN
        RAISE EXCEPTION 'public.context_audit_events.event_key must be TEXT'
            USING HINT = 'Repair the existing event_key type explicitly, then retry.';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.context_audit_events
        WHERE event_key IS NOT NULL
        GROUP BY event_key HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'public.context_audit_events has duplicate non-NULL event_key values'
            USING HINT = 'Reconcile duplicate keys explicitly before retrying; no rows were deleted.';
    END IF;
END
$require_context_audit_event_keys$;

CREATE UNIQUE INDEX IF NOT EXISTS ux_context_audit_events_event_key
    ON public.context_audit_events (event_key);

COMMENT ON COLUMN public.context_audit_events.event_key IS
    'Deterministic key of the source event: sha256 of topic:partition:offset in UUID shape. NULL on rows written before node_projection_context_audit.';

RESET lock_timeout;
RESET statement_timeout;
