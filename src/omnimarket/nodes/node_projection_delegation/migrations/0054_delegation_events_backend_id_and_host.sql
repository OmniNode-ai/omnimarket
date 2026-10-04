-- OMN-20162: delegation_events names the backend and host that served the answer.
--
-- backend_id and host come from the accepting attempt of a delegation terminal
-- (the first rung whose quality gate passed and that carries no failure class).
-- answering_backend (0052) is the terminal's route, a different datum, and is
-- left as it is.
--
-- Both columns are nullable with no default: rows written before this
-- migration, and terminals with no accepted attempt, read NULL. A blank value
-- is stored as NULL by the writer, never as an empty backend. This
-- metadata-only migration does not rewrite existing rows.

BEGIN;

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS backend_id TEXT,
    ADD COLUMN IF NOT EXISTS host TEXT;

COMMIT;
