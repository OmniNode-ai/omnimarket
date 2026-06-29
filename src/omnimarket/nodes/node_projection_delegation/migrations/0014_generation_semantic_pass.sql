-- OMN-13166: persist the behavioral (semantic) verdict on generation_events,
-- separate from the contract/shape verdict (contract_passed).
--
-- The 2026-06-16 gate-zero stability SEA cell generated a snake_case ->
-- PascalCase handler that split on whitespace instead of underscores
-- ("hello_world" -> "Hello_world"), yet the run was projected as
-- contract_passed=true. contract_passed only proves the artifact is SHAPED like
-- an ONEX node (valid YAML fields, parseable handler, no hardcoded paths/topics).
-- It says nothing about whether the handler performs the requested
-- transformation.
--
-- These columns record the behavioral verdict produced by the generation
-- consumer's semantic-validation layer, which executes the generated handle()
-- against synthesized input/expected fixtures derived from the task:
--   - semantic_checked: TRUE when a behavioral fixture was derivable for the
--                       task (i.e. the check was applicable). FALSE means the
--                       check was inconclusive (no known invariant) — which is
--                       NOT a behavioral pass.
--   - semantic_passed:  TRUE only when semantic_checked is TRUE AND the handler
--                       produced the correct output for every fixture.
--
-- A row with contract_passed=TRUE and semantic_passed=FALSE is the gate-zero
-- false-green made visible: shape-valid but behaviorally wrong.
--
-- Default FALSE backfills existing rows as "behavior not verified", which is the
-- honest historical state — they predate the semantic layer.
--
-- Applied only (never reversed); deploy to live lanes is user-gated.

ALTER TABLE generation_events
    ADD COLUMN IF NOT EXISTS semantic_checked BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS semantic_passed BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_generation_events_semantic_passed
    ON generation_events (semantic_passed);
