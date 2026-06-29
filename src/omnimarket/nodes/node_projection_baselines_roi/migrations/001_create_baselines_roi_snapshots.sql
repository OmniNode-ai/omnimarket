-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-13075: Baselines ROI snapshot projection table.
--
-- Backs the projection topic
--   onex.snapshot.projection.baselines.roi.v1
-- consumed by the omnidash BaselinesROICard widget (roi-trend, roi-by-model).
--
-- Source: per-snapshot ModelBaselinesComputedEvent carried on the topic
-- onex.evt.omnibase-infra.baselines-computed.v1.
-- One projection row per snapshot_id (upserted; latest per snapshot wins).
-- Fields:
--   token_delta   — sum of comparison.token_delta across all comparisons.
--   time_delta_ms — sum of comparison.time_delta_s * 1000 across all comparisons.
--   retry_delta   — sum of retry_count across all retry_counts.
--   recommendations — JSONB counting actions: promote / shadow / suppress / fork.
--   confidence    — average mapped confidence score across comparisons
--                   (high=1.0, medium=0.5, low=0.25; 0.0 when no comparisons).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS baselines_roi_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Identity — upsert conflict key
    snapshot_id TEXT NOT NULL,
    -- Source event timestamp
    captured_at TEXT NOT NULL,
    -- ROI aggregates
    token_delta BIGINT NOT NULL DEFAULT 0,
    time_delta_ms NUMERIC(18, 3) NOT NULL DEFAULT 0,
    retry_delta INTEGER NOT NULL DEFAULT 0 CHECK (retry_delta >= 0),
    -- Recommendation action counts stored as JSONB for structured access
    -- Shape: {"promote": N, "shadow": N, "suppress": N, "fork": N}
    recommendations JSONB NOT NULL DEFAULT '{"promote": 0, "shadow": 0, "suppress": 0, "fork": 0}',
    -- Average confidence score [0.0, 1.0]
    confidence NUMERIC(8, 6) NOT NULL DEFAULT 0 CHECK (confidence >= 0 AND confidence <= 1),
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
