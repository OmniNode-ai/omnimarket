-- OMN-15533 (AC3, second pass): persist the savings provenance the SOURCE EVENT
-- already states, so the read view stops inferring one.
--
-- WHY A SECOND PASS ON AC3
-- ------------------------
-- Migration 083 removed the hardcoded `'measured' AS savings_method` literal and
-- replaced it with a derivation from the persisted token counts:
--
--     served tokens > 0  ->  'measured'
--     otherwise          ->  'estimated' / 'unknown'
--
-- That made AC3's falsifying conjunction (measured + savings-estimated + zero
-- tokens) unreachable, but it did so by tying `measured` to the exact negation of
-- the conjunction's third clause. The label became unfalsifiable rather than
-- correct, and it is still WRONG in the one direction that matters: a row that
-- carries real token counts but whose saving was ESTIMATED now reads `measured`.
--
-- Token counts are a fact about the REQUEST. They are not evidence about how the
-- SAVING was computed. ModelSavingsEstimate.estimated_total_savings_usd is
-- `direct_savings_usd + heuristic_savings_usd` -- a heuristic-inclusive estimate --
-- and a row derived from it is an estimate no matter how many tokens the request
-- served. Deriving provenance from token presence is the projection layer
-- manufacturing a fact, which is the same defect class as the `'measured'` literal
-- it replaced (OMN-13355: savings = measurement, not estimate).
--
-- THE SOURCE ALREADY SAYS IT
-- --------------------------
-- The real producer of onex.evt.omnibase-infra.savings-estimated.v1
-- (omnibase_infra node_savings_estimation_compute, ModelSavingsEstimate) ships
-- three genuine provenance facts on every event, and to_kafka_payload() emits all
-- three:
--
--     is_measured             bool   -- "True if directly measured"
--     usage_source            text   -- "MEASURED, ESTIMATED, or UNKNOWN"
--     pricing_manifest_version text  -- the manifest the estimate was priced with
--
-- All three were discarded at the projection seam: the normalizer did not map
-- them, _CANONICAL_SAVINGS_FIELDS filtered them out, and savings_estimates had no
-- column to hold them. The view then invented replacements -- including a
-- hardcoded `'savings-estimated'` pricing_manifest_version, which names the SOURCE
-- TABLE rather than any manifest the run actually priced with.
--
-- These columns close that seam. Migration 087 repoints both views onto them.
--
-- NULLABLE, NO DEFAULT -- DELIBERATE (same rule as migration 082)
-- --------------------------------------------------------------
-- A row written before this migration recorded no provenance. A DEFAULT would
-- manufacture a claim that was never made. NULL means "the source did not state
-- this", and migration 087 reads it as a refusal: an unrecorded provenance renders
-- `estimated` / `unknown`, never `measured`. Claiming measurement requires the
-- source to have claimed it.

ALTER TABLE public.savings_estimates ADD COLUMN IF NOT EXISTS savings_method TEXT;
ALTER TABLE public.savings_estimates ADD COLUMN IF NOT EXISTS usage_source TEXT;
ALTER TABLE public.savings_estimates
    ADD COLUMN IF NOT EXISTS pricing_manifest_version TEXT;

-- The vocabulary is the consumer contract's, not free text: omnidash
-- delegation-savings.types.ts declares
--   savings_method: 'measured' | 'estimated'
--   usage_source:   'measured' | 'estimated' | 'unknown'
-- Constraining the column is what lets migration 087 pass the persisted value
-- straight through instead of re-deriving a safe one. NULL ("not recorded") stays
-- permitted and is resolved by the view, not by a default here.
ALTER TABLE public.savings_estimates
    DROP CONSTRAINT IF EXISTS savings_estimates_savings_method_check;
ALTER TABLE public.savings_estimates
    ADD CONSTRAINT savings_estimates_savings_method_check
    CHECK (savings_method IS NULL OR savings_method IN ('measured', 'estimated'))
    NOT VALID;

ALTER TABLE public.savings_estimates
    DROP CONSTRAINT IF EXISTS savings_estimates_usage_source_check;
ALTER TABLE public.savings_estimates
    ADD CONSTRAINT savings_estimates_usage_source_check
    CHECK (usage_source IS NULL
           OR usage_source IN ('measured', 'estimated', 'unknown'))
    NOT VALID;

COMMENT ON COLUMN public.savings_estimates.savings_method IS
    'OMN-15533: how the SAVING was obtained, as stated by the source event '
    '(ModelSavingsEstimate.is_measured / a delegation terminal re-derived from '
    'served tokens). NULL = not recorded; the read view renders that as '
    'estimated, never measured. Never inferred from token counts.';
COMMENT ON COLUMN public.savings_estimates.usage_source IS
    'OMN-15533: cost provenance as stated by the source event '
    '(ModelSavingsEstimate.usage_source). NULL = not recorded; the read view '
    'renders that as unknown.';
COMMENT ON COLUMN public.savings_estimates.pricing_manifest_version IS
    'OMN-15533: the pricing manifest the source event priced with. NULL = not '
    'recorded. Replaces the hardcoded savings-estimated literal, which named the '
    'source table rather than any manifest.';
