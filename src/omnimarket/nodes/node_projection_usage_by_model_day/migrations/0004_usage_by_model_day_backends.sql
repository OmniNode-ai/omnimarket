-- OMN-25861: each usage row serves the backends its calls were reached through.
-- Owner: omnimarket.nodes.node_projection_usage_by_model_day
-- Target database: omnidash_analytics; physical schema: public.
--
-- Additive only, and nullable with no default (JG-19): a call stored before this
-- migration, or one whose event named no backend, reads backend NULL, meaning the
-- source stated nothing; a row whose calls include one reads backends NULL. A row
-- whose every call stated its backend reads the sorted distinct backends that
-- differ from the row's model (a direct call adds nothing), as a JSON array, which
-- may be empty. The writer recounts a row's backends with its other figures on the
-- row's next call; nothing is backfilled, since nothing recorded a stored call's
-- backend. The writer follows the columns in force, so the writer that runs on lanes
-- today keeps writing, and a rollback needs no data change.
-- Re-runnable: every statement is IF NOT EXISTS.

ALTER TABLE public.usage_by_model_day_calls ADD COLUMN IF NOT EXISTS backend TEXT;
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS backends JSONB;
