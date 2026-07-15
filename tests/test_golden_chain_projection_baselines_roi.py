# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage for node_projection_baselines_roi."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from omnibase_infra.services.observability.baselines.models.model_baselines_comparison_row import (
    ModelBaselinesComparisonRow,
)
from omnibase_infra.services.observability.baselines.models.model_baselines_snapshot_event import (
    ModelBaselinesSnapshotEvent,
)

from omnimarket.nodes.node_projection_baselines_roi.handlers.handler_projection_baselines_roi import (
    HandlerProjectionBaselinesRoi,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_NOW = datetime(2026, 6, 28, 10, 0, 0, tzinfo=UTC)


@pytest.mark.unit
def test_golden_chain_baselines_snapshot_event_projects_roi_snapshot() -> None:
    db = InmemoryDatabaseAdapter()
    event = ModelBaselinesSnapshotEvent(
        snapshot_id=UUID("11111111-1111-1111-1111-111111111111"),
        computed_at_utc=_NOW,
        comparisons=[
            ModelBaselinesComparisonRow(
                id=UUID("22222222-2222-2222-2222-222222222222"),
                comparison_date=date(2026, 6, 27),
                treatment_total_tokens=6000,
                control_total_tokens=8000,
                roi_pct=25.0,
                latency_improvement_pct=30.0,
                cost_improvement_pct=25.0,
                sample_size=100,
                computed_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
            ),
            ModelBaselinesComparisonRow(
                id=UUID("33333333-3333-3333-3333-333333333333"),
                comparison_date=date(2026, 6, 28),
                treatment_total_tokens=4000,
                control_total_tokens=4500,
                roi_pct=10.0,
                latency_improvement_pct=15.0,
                cost_improvement_pct=11.1,
                sample_size=50,
                computed_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
            ),
        ],
    )

    result = HandlerProjectionBaselinesRoi().project(event, db)

    assert result.rows_upserted == 1
    row = db.query("baselines_roi_snapshots")[0]
    assert row["snapshot_id"] == "11111111-1111-1111-1111-111111111111"
    assert row["captured_at"] == _NOW.isoformat()
    # (8000 + 4500) control - (6000 + 4000) treatment = 2500
    assert row["token_delta"] == 2500
    assert float(str(row["roi_pct_avg"])) == pytest.approx(17.5)
    assert float(str(row["latency_improvement_pct_avg"])) == pytest.approx(22.5)
    assert float(str(row["cost_improvement_pct_avg"])) == pytest.approx(18.05)
    assert row["sample_size"] == 150
