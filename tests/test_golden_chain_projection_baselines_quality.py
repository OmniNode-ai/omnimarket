# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_projection_baselines_quality — NC-03.

Exercises the full live path: event → handler → DB upsert → projection row.
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_baselines.handlers.handler_projection_baselines import (
    ModelBaselinesComparison,
    ModelBaselinesComputedEvent,
)
from omnimarket.nodes.node_projection_baselines_quality.handlers.handler_projection_baselines_quality import (
    HandlerProjectionBaselinesQuality,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionBaselinesQuality()
TABLE = "baselines_quality_snapshots"


class TestBaselinesQualityProjection:
    def test_project_empty_event_writes_zero_quality(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesComputedEvent(
            snapshot_id="snap-q-001",
            computed_at_utc="2026-06-28T10:00:00Z",
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query(TABLE)
        assert len(rows) == 1
        assert rows[0]["quality_score"] == 0.0

    def test_project_all_high_confidence_is_perfect_score(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesComputedEvent(
            snapshot_id="snap-q-002",
            computed_at_utc="2026-06-28T10:00:00Z",
            patterns_compared=2,
            patterns_recommended=2,
            comparisons=[
                ModelBaselinesComparison(pattern_id="p1", confidence="high"),
                ModelBaselinesComparison(pattern_id="p2", confidence="high"),
            ],
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query(TABLE)
        assert abs(float(str(rows[0]["quality_score"])) - 1.0) < 0.0001
        assert float(str(rows[0]["recommend_rate"])) == 1.0

    def test_project_mixed_confidence_tiers(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesComputedEvent(
            snapshot_id="snap-q-003",
            computed_at_utc="2026-06-28T10:00:00Z",
            patterns_compared=3,
            patterns_recommended=1,
            comparisons=[
                ModelBaselinesComparison(pattern_id="a", confidence="high"),
                ModelBaselinesComparison(pattern_id="b", confidence="medium"),
                ModelBaselinesComparison(pattern_id="c", confidence="low"),
            ],
        )
        HANDLER.project(event, db)
        row = db.query(TABLE)[0]
        assert row["high_confidence_count"] == 1
        assert row["medium_confidence_count"] == 1
        assert row["low_confidence_count"] == 1
        # (1.0 + 0.5 + 0.25) / 3 = 0.5833...
        assert abs(float(str(row["quality_score"])) - (1.75 / 3)) < 0.0001

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
