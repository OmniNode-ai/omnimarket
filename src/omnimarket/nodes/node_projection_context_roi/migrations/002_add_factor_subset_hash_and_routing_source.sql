-- OMN-14535: context_roi_scores was silently dropping two fields the runner
-- has emitted on every ModelAttemptReductionRow since their introduction
-- (node_context_roi_runner/handlers/handler_context_roi_runner.py):
--   - factor_subset_hash: SHA-256 hex digest of the ordered factor-subset
--     label set for the arm (replay-audits which exact factor combination
--     produced the row).
--   - routing_source: provenance of the model/endpoint selection for the
--     row (e.g. 'routing_tier:local-coder'), echoed from the routing
--     authority or derived from endpoint_class.
--
-- The projection handler never captured either field into the INSERT/UPSERT
-- row, and the table had no columns for them — 100% silent loss on every
-- row ever written, not just a code gap. Both fields exist specifically so
-- downstream "winning factor" analysis is not a routing artifact (BAC plan
-- line 111); every row written before this migration cannot distinguish
-- "field never captured" from "legitimately empty" (e.g. the 'off' arm's
-- factor_subset_hash is also '' by design) — see the handler-side comment
-- for the same caveat. Not backfillable: the real values were never
-- persisted anywhere to recover from.

ALTER TABLE context_roi_scores
    ADD COLUMN IF NOT EXISTS factor_subset_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS routing_source TEXT NOT NULL DEFAULT '';
