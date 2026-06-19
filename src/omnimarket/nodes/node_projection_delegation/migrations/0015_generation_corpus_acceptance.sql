-- OMN-13350: persist the validator-acceptance (corpus) verdict on
-- generation_events, alongside the contract (shape) and semantic (behavioral)
-- verdicts.
--
-- Root cause this migration closes: the omnimarket 0.4.3 generation handler
-- (HandlerGenerationConsumer._emit_benchmark) emits the full
-- ModelGenerationBenchmark.model_dump() on
-- onex.evt.omnimarket.node-generation-completed.v1. As of OMN-13289 that payload
-- carries corpus_checked / corpus_passed / corpus_errors, but generation_events
-- had no such columns, so the projection INSERT raised UndefinedColumn and every
-- completion event was dropped (the consumer committed the offset anyway, so the
-- group reported Stable / LAG=0 while projecting zero rows).
--
-- Field semantics (OMN-13289), all populated by the projection write path:
--   - corpus_checked: TRUE only when the generation run carried a validator
--                     acceptance corpus (a validator-generation run). FALSE is
--                     ordinary free-text generation (no corpus gate) — which is
--                     NOT a corpus pass.
--   - corpus_passed:  TRUE only when corpus_checked is TRUE AND the generated
--                     scanner flagged every violation_fixture AND produced zero
--                     findings on every clean_fixture, by deterministic corpus
--                     execution. The corpus — not the LLM — is the acceptance
--                     authority. Never TRUE for a run without a corpus.
--   - corpus_errors:  per-fixture acceptance failures (which violation_fixture
--                     was missed, which clean_fixture was false-flagged). Empty
--                     JSON array on a corpus pass or a run with no corpus.
--
-- A row with contract_passed=TRUE and corpus_passed=FALSE on a corpus-checked
-- run is a shape-valid validator that does NOT actually enforce the rule — it
-- did not deploy, and its corpus failures fed the repair loop.
--
-- Default FALSE / '[]' backfills existing rows as "corpus not verified", which is
-- the honest historical state — they predate the corpus-acceptance layer.
--
-- Applied only (never reversed); deploy to live lanes is user-gated.

ALTER TABLE generation_events
    ADD COLUMN IF NOT EXISTS corpus_checked BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS corpus_passed BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS corpus_errors JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_generation_events_corpus_passed
    ON generation_events (corpus_passed);
