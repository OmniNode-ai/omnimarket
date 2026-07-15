-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-14630: realign baselines_quality_snapshots to the real producer schema.
--
-- WHY THIS EXISTS
--   001 shaped this table around a fictional local model
--   (patterns_compared/patterns_recommended/per-comparison string
--   confidence tiers) invented independently of the real producer contract
--   and sharing almost no field names with it. The real producer is
--   omnibase_infra.services.observability.baselines.models
--   .model_baselines_snapshot_event.ModelBaselinesSnapshotEvent, whose
--   breakdown rows (ModelBaselinesBreakdownRow) carry a numeric
--   sample-sufficiency confidence proxy (non-null only when
--   sample_count >= 20) and comparison rows (ModelBaselinesComparisonRow)
--   carry treatment_success_rate directly. See OMN-14513 for the sibling
--   node_projection_baselines fix and OMN-14630 for this node's own
--   field-overlap analysis.
--
--   Confirmed live on .201 stability-test (2026-07-14): the
--   projection_baselines_quality consumer group had committed offset 2
--   with zero lag (both real events on the topic consumed) yet
--   baselines_quality_snapshots held zero rows -- every real event silently
--   crashed inside the runtime's catch-all dispatch boundary since this
--   node was deployed.
--
-- Field changes:
--   patterns_compared        — redefined as len(event.breakdown); same name.
--   patterns_recommended     — renamed to patterns_significant (count of
--                               breakdown rows with confidence is not null,
--                               i.e. sample_count >= 20; the real event has
--                               no "recommendation" concept).
--   high/medium/low_confidence_count — redefined from breakdown.confidence
--                               thresholds instead of a fictional string
--                               tier (see handler docstring for thresholds).
--   quality_score             — redefined as mean(comparison
--                               .treatment_success_rate); real producer
--                               field.
--   recommend_rate            — renamed to significant_rate
--                               (patterns_significant / patterns_compared).
--
-- Recreated, not ALTERed: this table holds zero rows in every known
-- environment (verified live on .201 stability-test omnidash_analytics), so
-- there is no data to preserve across the shape change.
--
-- Idempotency: guarded with IF EXISTS / IF NOT EXISTS so this migration is
-- safe to re-apply.

DROP TABLE IF EXISTS baselines_quality_snapshots;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS baselines_quality_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Identity — upsert conflict key
    snapshot_id TEXT NOT NULL,
    -- Source event timestamp
    captured_at TEXT NOT NULL,
    -- Pattern counts from the snapshot's breakdown rows
    patterns_compared INTEGER NOT NULL DEFAULT 0 CHECK (patterns_compared >= 0),
    patterns_significant INTEGER NOT NULL DEFAULT 0 CHECK (patterns_significant >= 0),
    -- Confidence tier counts (derived from breakdown.confidence thresholds)
    high_confidence_count INTEGER NOT NULL DEFAULT 0 CHECK (high_confidence_count >= 0),
    medium_confidence_count INTEGER NOT NULL DEFAULT 0 CHECK (medium_confidence_count >= 0),
    low_confidence_count INTEGER NOT NULL DEFAULT 0 CHECK (low_confidence_count >= 0),
    -- Mean of non-null comparison.treatment_success_rate
    quality_score NUMERIC(8, 6) NOT NULL DEFAULT 0 CHECK (quality_score >= 0 AND quality_score <= 1),
    -- patterns_significant / max(1, patterns_compared)
    significant_rate NUMERIC(8, 6) NOT NULL DEFAULT 0 CHECK (significant_rate >= 0 AND significant_rate <= 1),
    -- Projection metadata
    projected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_baselines_quality_snapshots_snapshot_id
    ON baselines_quality_snapshots (snapshot_id);

CREATE INDEX IF NOT EXISTS ix_baselines_quality_snapshots_projected_at
    ON baselines_quality_snapshots (projected_at DESC);

CREATE OR REPLACE FUNCTION refresh_baselines_quality_snapshots_projected_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.projected_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_baselines_quality_snapshots_projected_at ON baselines_quality_snapshots;
CREATE TRIGGER trg_baselines_quality_snapshots_projected_at
    BEFORE UPDATE ON baselines_quality_snapshots
    FOR EACH ROW
    EXECUTE FUNCTION refresh_baselines_quality_snapshots_projected_at();
