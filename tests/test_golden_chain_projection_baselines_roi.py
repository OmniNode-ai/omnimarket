# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage for node_projection_baselines_roi."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_projection_baselines.handlers.handler_projection_baselines import (
    ModelBaselinesComparison,
    ModelBaselinesComputedEvent,
    ModelBaselinesRecommendation,
    ModelBaselinesRetryCount,
)
from omnimarket.nodes.node_projection_baselines_roi.handlers.handler_projection_baselines_roi import (
    HandlerProjectionBaselinesRoi,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter


@pytest.mark.unit
def test_golden_chain_baselines_computed_event_projects_roi_snapshot() -> None:
    db = InmemoryDatabaseAdapter()
    event = ModelBaselinesComputedEvent(
        snapshot_id="golden-roi-001",
        computed_at_utc="2026-06-28T10:00:00Z",
        patterns_compared=3,
        patterns_recommended=2,
        comparisons=[
            ModelBaselinesComparison(
                pattern_id="pattern-a",
                token_delta=-2000,
                time_delta_s=1.5,
                confidence="high",
            ),
            ModelBaselinesComparison(
                pattern_id="pattern-b",
                token_delta=500,
                time_delta_s=-0.3,
                confidence="medium",
            ),
        ],
        recommendations=[
            ModelBaselinesRecommendation(
                pattern_id="pattern-a",
                action="promote",
                confidence="high",
            ),
            ModelBaselinesRecommendation(
                pattern_id="pattern-b",
                action="shadow",
                confidence="medium",
            ),
        ],
        retry_counts=[
            ModelBaselinesRetryCount(pattern_id="pattern-a", retry_count=3),
            ModelBaselinesRetryCount(pattern_id="pattern-b", retry_count=1),
        ],
    )

    result = HandlerProjectionBaselinesRoi().project(event, db)

    assert result.rows_upserted == 1
    row = db.query("baselines_roi_snapshots")[0]
    assert row["snapshot_id"] == "golden-roi-001"
    assert row["captured_at"] == "2026-06-28T10:00:00Z"
    assert row["token_delta"] == -1500
    assert float(str(row["time_delta_ms"])) == pytest.approx(1200.0)
    assert row["retry_delta"] == 4
    assert row["recommendations"] == {
        "promote": 1,
        "shadow": 1,
        "suppress": 0,
        "fork": 0,
    }
    assert float(str(row["confidence"])) == pytest.approx(0.75)
