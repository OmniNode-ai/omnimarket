# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_projection_baselines_quality — NC-03.

Exercises the full live path: event → handler → DB upsert → projection row.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from omnibase_infra.services.observability.baselines.models.model_baselines_breakdown_row import (
    ModelBaselinesBreakdownRow,
)
from omnibase_infra.services.observability.baselines.models.model_baselines_comparison_row import (
    ModelBaselinesComparisonRow,
)
from omnibase_infra.services.observability.baselines.models.model_baselines_snapshot_event import (
    ModelBaselinesSnapshotEvent,
)

from omnimarket.nodes.node_projection_baselines_quality.handlers.handler_projection_baselines_quality import (
    HandlerProjectionBaselinesQuality,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionBaselinesQuality()
TABLE = "baselines_quality_snapshots"
_NOW = datetime(2026, 6, 28, 10, 0, 0, tzinfo=UTC)


def _breakdown(idx: int, confidence: float | None) -> ModelBaselinesBreakdownRow:
    return ModelBaselinesBreakdownRow(
        id=UUID(f"{idx:08d}-0000-0000-0000-000000000000"),
        pattern_id=UUID(f"{idx:08d}-1111-1111-1111-111111111111"),
        confidence=confidence,
        sample_count=25 if confidence is not None else 5,
        computed_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )


class TestBaselinesQualityProjection:
    def test_project_empty_event_writes_zero_quality(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesSnapshotEvent(
            snapshot_id=UUID("00000000-0000-0000-0000-000000000001"),
            computed_at_utc=_NOW,
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query(TABLE)
        assert len(rows) == 1
        assert rows[0]["quality_score"] == 0.0

    def test_project_all_high_confidence_is_perfect_score(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesSnapshotEvent(
            snapshot_id=UUID("00000000-0000-0000-0000-000000000002"),
            computed_at_utc=_NOW,
            comparisons=[
                ModelBaselinesComparisonRow(
                    id=UUID("10000000-0000-0000-0000-000000000000"),
                    comparison_date=date(2026, 6, 28),
                    treatment_success_rate=1.0,
                    computed_at=_NOW,
                    created_at=_NOW,
                    updated_at=_NOW,
                ),
            ],
            breakdown=[_breakdown(1, 0.9), _breakdown(2, 0.85)],
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query(TABLE)
        assert abs(float(str(rows[0]["quality_score"])) - 1.0) < 0.0001
        assert float(str(rows[0]["significant_rate"])) == 1.0

    def test_project_mixed_confidence_tiers(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesSnapshotEvent(
            snapshot_id=UUID("00000000-0000-0000-0000-000000000003"),
            computed_at_utc=_NOW,
            comparisons=[
                ModelBaselinesComparisonRow(
                    id=UUID("10000000-0000-0000-0000-000000000001"),
                    comparison_date=date(2026, 6, 28),
                    treatment_success_rate=0.5,
                    computed_at=_NOW,
                    created_at=_NOW,
                    updated_at=_NOW,
                ),
            ],
            breakdown=[
                _breakdown(1, 0.9),  # high
                _breakdown(2, 0.6),  # medium
                _breakdown(3, None),  # low (insufficient sample)
            ],
        )
        HANDLER.project(event, db)
        row = db.query(TABLE)[0]
        assert row["high_confidence_count"] == 1
        assert row["medium_confidence_count"] == 1
        assert row["low_confidence_count"] == 1
        assert row["patterns_compared"] == 3
        assert row["patterns_significant"] == 2

    def test_event_bus_wiring(self) -> None:
        import yaml

        contract_path = (
            "src/omnimarket/nodes/node_projection_baselines_quality/contract.yaml"
        )
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert (
            "onex.evt.omnibase-infra.baselines-computed.v1"
            in contract["event_bus"]["subscribe_topics"]
        )
        exposures = contract["projection_api"]["exposures"]
        assert any(
            e["topic"] == "onex.snapshot.projection.baselines.quality.v1"
            for e in exposures
        )
        assert (
            contract["terminal_event"]
            == "onex.evt.omnimarket.projection-baselines-quality-applied.v1"
        )
