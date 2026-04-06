"""Golden chain tests for node_projection_baselines."""

from __future__ import annotations

import yaml

from omnimarket.nodes.node_projection_baselines.handlers.handler_projection_baselines import (
    HandlerProjectionBaselines,
    ModelBaselinesComparison,
    ModelBaselinesComputedEvent,
    ModelBaselinesRecommendation,
    ModelBaselinesRetryCount,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionBaselines()


class TestBaselinesProjection:
    def test_project_snapshot_only(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesComputedEvent(
            snapshot_id="snap-001",
            computed_at_utc="2026-04-06T10:00:00Z",
            patterns_compared=5,
            patterns_recommended=2,
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        assert "baselines_snapshots" in result.tables_written
        rows = db.query("baselines_snapshots")
        assert len(rows) == 1
        assert rows[0]["patterns_compared"] == 5

    def test_project_with_comparisons(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesComputedEvent(
            snapshot_id="snap-002",
            computed_at_utc="2026-04-06T10:00:00Z",
            patterns_compared=2,
            comparisons=[
                ModelBaselinesComparison(
                    pattern_id="pat-a",
                    pattern_name="retry-guard",
                    sample_size=50,
                    baseline_tokens=10000,
                    current_tokens=8000,
                    token_delta=-2000,
                    confidence="high",
                ),
                ModelBaselinesComparison(
                    pattern_id="pat-b",
                    pattern_name="cache-hit",
                    sample_size=30,
                    baseline_tokens=5000,
                    current_tokens=5500,
                    token_delta=500,
                    confidence="medium",
                ),
            ],
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 3  # 1 snapshot + 2 comparisons
        assert "baselines_comparisons" in result.tables_written

    def test_project_with_recommendations(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesComputedEvent(
            snapshot_id="snap-003",
            computed_at_utc="2026-04-06T10:00:00Z",
            recommendations=[
                ModelBaselinesRecommendation(
                    pattern_id="pat-a",
                    action="promote",
                    reason="Consistent improvement",
                    confidence="high",
                ),
            ],
        )
        result = HANDLER.project(event, db)
        assert "baselines_recommendations" in result.tables_written
        recs = db.query("baselines_recommendations")
        assert len(recs) == 1
        assert recs[0]["action"] == "promote"

    def test_project_with_retry_counts(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesComputedEvent(
            snapshot_id="snap-004",
            computed_at_utc="2026-04-06T10:00:00Z",
            retry_counts=[
                ModelBaselinesRetryCount(
                    pattern_id="pat-a",
                    retry_count=3,
                    window_start="2026-04-01",
                    window_end="2026-04-06",
                ),
            ],
        )
        result = HANDLER.project(event, db)
        assert "baselines_retry_counts" in result.tables_written

    def test_full_event_all_tables(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesComputedEvent(
            snapshot_id="snap-full",
            computed_at_utc="2026-04-06T10:00:00Z",
            patterns_compared=1,
            patterns_recommended=1,
            comparisons=[
                ModelBaselinesComparison(pattern_id="p1", confidence="high"),
            ],
            recommendations=[
                ModelBaselinesRecommendation(pattern_id="p1", action="promote"),
            ],
            retry_counts=[
                ModelBaselinesRetryCount(pattern_id="p1", retry_count=2),
            ],
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 4  # 1 snapshot + 1 each child
        assert len(result.tables_written) == 4

    def test_upsert_snapshot_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelBaselinesComputedEvent(
                snapshot_id="snap-dup",
                computed_at_utc="2026-04-06T10:00:00Z",
                patterns_compared=1,
            ),
            db,
        )
        HANDLER.project(
            ModelBaselinesComputedEvent(
                snapshot_id="snap-dup",
                computed_at_utc="2026-04-06T11:00:00Z",
                patterns_compared=2,
            ),
            db,
        )
        rows = db.query("baselines_snapshots")
        assert len(rows) == 1
        assert rows[0]["patterns_compared"] == 2

    def test_event_bus_wiring(self) -> None:
        contract_path = "src/omnimarket/nodes/node_projection_baselines/contract.yaml"
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert (
            "onex.evt.omnibase-infra.baselines-computed.v1"
            in contract["event_bus"]["subscribe_topics"]
        )
