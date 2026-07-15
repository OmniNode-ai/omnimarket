-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-14630: realign baselines_roi_snapshots to the real producer schema.
--
-- WHY THIS EXISTS
--   001 shaped this table around a fictional per-pattern local model
--   (pattern_id/token_delta/time_delta_s/confidence/recommendations/
--   retry_counts) invented independently of the real producer contract and
--   sharing almost no field names with it. The real producer is
--   omnibase_infra.services.observability.baselines.models
--   .model_baselines_snapshot_event.ModelBaselinesSnapshotEvent, whose
--   comparison rows are ModelBaselinesComparisonRow (daily treatment-vs-
--   control shapes keyed by comparison_date, not pattern_id). See OMN-14513
--   for the sibling node_projection_baselines fix and OMN-14630 for this
--   node's own field-overlap analysis.
--
--   Confirmed live on .201 stability-test (2026-07-14): the
--   projection_baselines_roi consumer group had committed offset 2 with
--   zero lag (both real events on the topic consumed) yet
--   baselines_roi_snapshots held zero rows -- every real event silently
--   crashed inside the runtime's catch-all dispatch boundary since this
--   node was deployed.
--
-- New fields (no producer analog -> dropped, not remapped 1:1):
--   time_delta_ms, retry_delta, recommendations, confidence
-- Retained fields (redefined onto real producer-native fields):
--   token_delta = sum(control_total_tokens) - sum(treatment_total_tokens)
-- New fields (real producer-native aggregates):
--   roi_pct_avg, latency_improvement_pct_avg, cost_improvement_pct_avg,
--   sample_size
--
-- Recreated, not ALTERed: this table holds zero rows in every known
-- environment (verified live on .201 stability-test omnidash_analytics), so
-- there is no data to preserve across the shape change.
--
-- Idempotency: guarded with IF EXISTS / IF NOT EXISTS so this migration is
-- safe to re-apply.

DROP TABLE IF EXISTS baselines_roi_snapshots;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS baselines_roi_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Identity — upsert conflict key
    snapshot_id TEXT NOT NULL,
    -- Source event timestamp
    captured_at TEXT NOT NULL,
    -- Token savings: sum(control_total_tokens) - sum(treatment_total_tokens)
    -- across comparisons. Positive = treatment cohort used fewer tokens.
    token_delta BIGINT NOT NULL DEFAULT 0,
    -- Mean of non-null comparison.roi_pct
    roi_pct_avg DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- Mean of non-null comparison.latency_improvement_pct
    latency_improvement_pct_avg DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- Mean of non-null comparison.cost_improvement_pct
    cost_improvement_pct_avg DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- Sum of comparison.sample_size across all comparisons
    sample_size BIGINT NOT NULL DEFAULT 0 CHECK (sample_size >= 0),
    -- Projection metadata
    projected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_baselines_roi_snapshots_snapshot_id
    ON baselines_roi_snapshots (snapshot_id);

CREATE INDEX IF NOT EXISTS ix_baselines_roi_snapshots_projected_at
    ON baselines_roi_snapshots (projected_at DESC);

CREATE OR REPLACE FUNCTION refresh_baselines_roi_snapshots_projected_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.projected_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_baselines_roi_snapshots_projected_at ON baselines_roi_snapshots;
CREATE TRIGGER trg_baselines_roi_snapshots_projected_at
    BEFORE UPDATE ON baselines_roi_snapshots
    FOR EACH ROW
    EXECUTE FUNCTION refresh_baselines_roi_snapshots_projected_at();
