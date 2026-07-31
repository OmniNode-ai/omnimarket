-- OMN-14974: Repair persisted live-event lifecycle classifications.
--
-- The original reducer collapsed every topic containing "delegation" into
-- ROUTING and allowed payload ``type`` / ``event_type`` fields to override the
-- contract-declared topic.  The reducer now derives lifecycle type from the
-- topic; this migration applies the same ordered taxonomy to durable rows that
-- were projected before that fix.
--
-- This update is idempotent.  It touches only rows whose stored type differs
-- from the type implied by their authoritative topic.

WITH classified AS (
  SELECT
    id,
    CASE
      WHEN lower(topic) LIKE '%failed%' OR lower(topic) LIKE '%error%'
        THEN 'ERROR'
      WHEN lower(topic) LIKE 'onex.cmd.%'
        THEN 'COMMAND'
      WHEN lower(topic) ~ '\.routing-decision\.v[0-9]+$'
        THEN 'ROUTING'
      WHEN lower(topic) ~ '\.inference-response\.v[0-9]+$'
        THEN 'INFERENCE'
      WHEN lower(topic) ~ '\.(quality-gate-result|delegation-judge-verdict)\.v[0-9]+$'
        THEN 'EVALUATION'
      WHEN lower(topic) ~ '\.(delegation[^.]*|delegate-skill-[^.]+|task-delegated)\.v[0-9]+$'
        THEN 'DELEGATION'
      WHEN lower(topic) LIKE '%state-change%' OR lower(topic) LIKE '%transformation%'
        THEN 'TRANSFORMATION'
      ELSE 'ACTION'
    END AS canonical_type
  FROM live_events
)
UPDATE live_events AS event
SET type = classified.canonical_type
FROM classified
WHERE event.id = classified.id
  AND event.type IS DISTINCT FROM classified.canonical_type;
