-- OMN-14974: reconcile warm delegation_events tables created before the
-- contract-driven projection schema. This migration intentionally sorts after
-- the base/metrics migrations and before views that require model_name.

-- This is an explicitly bounded maintenance operation. PostgreSQL must acquire
-- the table lock before making any schema change; a busy writer makes the
-- migration fail within five seconds so the runner can retry without leaving a
-- partial conversion. The staging table was measured at 6 rows / 128 kB before
-- rollout, so the in-place JSONB-to-integer rewrite is deliberately preferred
-- over a permanent dual-column compatibility path.
-- The runner (scripts/run-forward-migrations.sh) applies node migrations with
-- `psql -f` in autocommit, one statement per implicit transaction. The SET LOCAL
-- scoping and the LOCK TABLE below are only legal inside an explicit transaction
-- block, and the partial-conversion guarantee described above requires one, so
-- this migration opens its own (OMN-15312).
BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';
LOCK TABLE delegation_events IN ACCESS EXCLUSIVE MODE;

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS model_name TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS llm_call_id TEXT,
    ADD COLUMN IF NOT EXISTS prompt_text TEXT,
    ADD COLUMN IF NOT EXISTS response_text TEXT,
    ADD COLUMN IF NOT EXISTS tokens_to_compliance INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS compliance_attempts INT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS quality_gates_checked_jsonb JSONB,
    ADD COLUMN IF NOT EXISTS quality_gates_failed_jsonb JSONB;

-- Old tables required callers to provide id explicitly, while every current
-- projection writer relies on the canonical UUID default.
ALTER TABLE delegation_events
    ALTER COLUMN id SET DEFAULT gen_random_uuid();

-- Legacy installations stored gate labels directly in the
-- quality_gates_checked/failed JSONB columns. Current handlers store integer
-- counts there and preserve the labels in the *_jsonb evidence columns.
DO $reconcile_checked$
DECLARE
    current_type TEXT;
    unsupported_shape_count BIGINT;
BEGIN
    SELECT data_type
      INTO current_type
      FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name = 'delegation_events'
       AND column_name = 'quality_gates_checked';

    IF current_type IS NULL THEN
        ALTER TABLE delegation_events
            ADD COLUMN quality_gates_checked INTEGER NOT NULL DEFAULT 0;
    ELSIF current_type = 'jsonb' THEN
        UPDATE delegation_events
           SET quality_gates_checked_jsonb =
               COALESCE(quality_gates_checked_jsonb, quality_gates_checked)
         WHERE quality_gates_checked IS NOT NULL;

        SELECT COUNT(*)
          INTO unsupported_shape_count
          FROM delegation_events
         WHERE quality_gates_checked IS NOT NULL
           AND NOT (
               jsonb_typeof(quality_gates_checked) = 'array'
               OR (
                   jsonb_typeof(quality_gates_checked) = 'number'
                   AND (quality_gates_checked #>> '{}') ~ '^[0-9]+$'
                   AND (quality_gates_checked #>> '{}')::NUMERIC <= 2147483647
               )
           );
        IF unsupported_shape_count > 0 THEN
            RAISE WARNING
                'delegation_events.quality_gates_checked has % row(s) with an unsupported JSONB shape; original values are preserved in quality_gates_checked_jsonb and counts are coerced to zero',
                unsupported_shape_count;
        END IF;

        ALTER TABLE delegation_events
            ALTER COLUMN quality_gates_checked DROP DEFAULT;
        EXECUTE $sql$
            ALTER TABLE delegation_events
                ALTER COLUMN quality_gates_checked TYPE INTEGER
                USING (
                    CASE
                        WHEN quality_gates_checked IS NULL THEN 0
                        WHEN jsonb_typeof(quality_gates_checked) = 'array'
                            THEN jsonb_array_length(quality_gates_checked)
                        WHEN jsonb_typeof(quality_gates_checked) = 'number'
                             AND (quality_gates_checked #>> '{}') ~ '^[0-9]+$'
                             AND (quality_gates_checked #>> '{}')::NUMERIC <= 2147483647
                            THEN (quality_gates_checked #>> '{}')::INTEGER
                        ELSE 0
                    END
                )
        $sql$;
    ELSIF current_type <> 'integer' THEN
        RAISE EXCEPTION
            'delegation_events.quality_gates_checked has unsupported type %',
            current_type;
    END IF;

    UPDATE delegation_events
       SET quality_gates_checked = 0
     WHERE quality_gates_checked IS NULL;
    ALTER TABLE delegation_events
        ALTER COLUMN quality_gates_checked SET DEFAULT 0,
        ALTER COLUMN quality_gates_checked SET NOT NULL;
END
$reconcile_checked$;

DO $reconcile_failed$
DECLARE
    current_type TEXT;
    unsupported_shape_count BIGINT;
BEGIN
    SELECT data_type
      INTO current_type
      FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name = 'delegation_events'
       AND column_name = 'quality_gates_failed';

    IF current_type IS NULL THEN
        ALTER TABLE delegation_events
            ADD COLUMN quality_gates_failed INTEGER NOT NULL DEFAULT 0;
    ELSIF current_type = 'jsonb' THEN
        UPDATE delegation_events
           SET quality_gates_failed_jsonb =
               COALESCE(quality_gates_failed_jsonb, quality_gates_failed)
         WHERE quality_gates_failed IS NOT NULL;

        SELECT COUNT(*)
          INTO unsupported_shape_count
          FROM delegation_events
         WHERE quality_gates_failed IS NOT NULL
           AND NOT (
               jsonb_typeof(quality_gates_failed) = 'array'
               OR (
                   jsonb_typeof(quality_gates_failed) = 'number'
                   AND (quality_gates_failed #>> '{}') ~ '^[0-9]+$'
                   AND (quality_gates_failed #>> '{}')::NUMERIC <= 2147483647
               )
           );
        IF unsupported_shape_count > 0 THEN
            RAISE WARNING
                'delegation_events.quality_gates_failed has % row(s) with an unsupported JSONB shape; original values are preserved in quality_gates_failed_jsonb and counts are coerced to zero',
                unsupported_shape_count;
        END IF;

        ALTER TABLE delegation_events
            ALTER COLUMN quality_gates_failed DROP DEFAULT;
        EXECUTE $sql$
            ALTER TABLE delegation_events
                ALTER COLUMN quality_gates_failed TYPE INTEGER
                USING (
                    CASE
                        WHEN quality_gates_failed IS NULL THEN 0
                        WHEN jsonb_typeof(quality_gates_failed) = 'array'
                            THEN jsonb_array_length(quality_gates_failed)
                        WHEN jsonb_typeof(quality_gates_failed) = 'number'
                             AND (quality_gates_failed #>> '{}') ~ '^[0-9]+$'
                             AND (quality_gates_failed #>> '{}')::NUMERIC <= 2147483647
                            THEN (quality_gates_failed #>> '{}')::INTEGER
                        ELSE 0
                    END
                )
        $sql$;
    ELSIF current_type <> 'integer' THEN
        RAISE EXCEPTION
            'delegation_events.quality_gates_failed has unsupported type %',
            current_type;
    END IF;

    UPDATE delegation_events
       SET quality_gates_failed = 0
     WHERE quality_gates_failed IS NULL;
    ALTER TABLE delegation_events
        ALTER COLUMN quality_gates_failed SET DEFAULT 0,
        ALTER COLUMN quality_gates_failed SET NOT NULL;
END
$reconcile_failed$;

COMMIT;
