-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-13076: Baselines quality snapshot projection table.
--
-- Backs the projection topic
--   onex.snapshot.projection.baselines.quality.v1
-- consumed by the omnidash quality-baseline-panel widget.
--
-- Source: per-snapshot ModelBaselinesComputedEvent carried on the topic
-- onex.evt.omnibase-infra.baselines-computed.v1.
-- One projection row per snapshot_id (upserted; latest per snapshot wins).
-- Fields:
--   patterns_compared       — total patterns compared in the snapshot window.
--   patterns_recommended    — patterns for which a recommendation was produced.
--   high_confidence_count   — comparisons where confidence == "high".
--   medium_confidence_count — comparisons where confidence == "medium".
--   low_confidence_count    — comparisons where confidence == "low" (or unknown tier).
--   quality_score           — weighted avg: (high*1.0 + medium*0.5 + low*0.25)
--                             / max(1, total_comparisons).
--   recommend_rate          — patterns_recommended / max(1, patterns_compared).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS baselines_quality_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Identity — upsert conflict key
    snapshot_id TEXT NOT NULL,
    -- Source event timestamp
    captured_at TEXT NOT NULL,
    -- Pattern counts from the snapshot
    patterns_compared INTEGER NOT NULL DEFAULT 0 CHECK (patterns_compared >= 0),
    patterns_recommended INTEGER NOT NULL DEFAULT 0 CHECK (patterns_recommended >= 0),
    -- Confidence tier counts
    high_confidence_count INTEGER NOT NULL DEFAULT 0 CHECK (high_confidence_count >= 0),
    medium_confidence_count INTEGER NOT NULL DEFAULT 0 CHECK (medium_confidence_count >= 0),
    low_confidence_count INTEGER NOT NULL DEFAULT 0 CHECK (low_confidence_count >= 0),
    -- Derived quality metrics
    quality_score NUMERIC(8, 6) NOT NULL DEFAULT 0 CHECK (quality_score >= 0 AND quality_score <= 1),
    recommend_rate NUMERIC(8, 6) NOT NULL DEFAULT 0 CHECK (recommend_rate >= 0 AND recommend_rate <= 1),
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
