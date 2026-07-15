"""Golden chain tests for node_projection_baselines.

OMN-14513: these tests previously drove the handler's own slim local models
(``ModelBaselinesComputedEvent``/``ModelBaselinesComparison``/...), which
encoded a fictional wire shape (required ``pattern_id`` with no default,
``patterns_compared``/``patterns_recommended`` columns that do not exist on
``baselines_snapshots``) rather than the real producer contract. Rewritten to
drive the producer's own canonical model
(``omnibase_infra...ModelBaselinesSnapshotEvent``) through the handler, which
is what a real Kafka message decodes to.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import yaml
from omnibase_infra.services.observability.baselines.models.model_baselines_breakdown_row import (
    ModelBaselinesBreakdownRow,
)
from omnibase_infra.services.observability.baselines.models.model_baselines_comparison_row import (
    ModelBaselinesComparisonRow,
)
from omnibase_infra.services.observability.baselines.models.model_baselines_snapshot_event import (
    ModelBaselinesSnapshotEvent,
)
from omnibase_infra.services.observability.baselines.models.model_baselines_trend_row import (
    ModelBaselinesTrendRow,
)

from omnimarket.nodes.node_projection_baselines.handlers.handler_projection_baselines import (
    HandlerProjectionBaselines,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionBaselines()
NOW = datetime(2026, 4, 6, 10, 0, 0, tzinfo=UTC)


class TestBaselinesProjection:
    def test_project_snapshot_only(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesSnapshotEvent(
            snapshot_id=uuid4(),
            computed_at_utc=NOW,
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        assert "baselines_snapshots" in result.tables_written
        rows = db.query("baselines_snapshots")
        assert len(rows) == 1
        assert rows[0]["snapshot_id"] == str(event.snapshot_id)

    def test_project_with_comparisons(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesSnapshotEvent(
            snapshot_id=uuid4(),
            computed_at_utc=NOW,
            comparisons=[
                ModelBaselinesComparisonRow(
                    id=uuid4(),
                    comparison_date=date(2026, 4, 5),
                    treatment_sessions=50,
                    treatment_success_rate=0.9,
                    control_sessions=45,
                    control_success_rate=0.7,
                    roi_pct=28.5,
                    sample_size=95,
                    computed_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                ModelBaselinesComparisonRow(
                    id=uuid4(),
                    comparison_date=date(2026, 4, 6),
                    treatment_sessions=30,
                    control_sessions=28,
                    sample_size=58,
                    computed_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ],
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 3  # 1 snapshot + 2 comparisons
        assert "baselines_comparisons" in result.tables_written
        rows = db.query("baselines_comparisons")
        assert len(rows) == 2
        assert {r["treatment_sessions"] for r in rows} == {50, 30}
        assert rows[0]["roi_pct"] == 28.5

    def test_project_with_trend(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesSnapshotEvent(
            snapshot_id=uuid4(),
            computed_at_utc=NOW,
            trend=[
                ModelBaselinesTrendRow(
                    id=uuid4(),
                    trend_date=date(2026, 4, 5),
                    cohort="treatment",
                    session_count=40,
                    success_rate=0.88,
                    computed_at=NOW,
                    created_at=NOW,
                ),
            ],
        )
        result = HANDLER.project(event, db)
        assert "baselines_trend" in result.tables_written
        rows = db.query("baselines_trend")
        assert len(rows) == 1
        assert rows[0]["cohort"] == "treatment"
        assert rows[0]["session_count"] == 40

    def test_project_with_breakdown(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesSnapshotEvent(
            snapshot_id=uuid4(),
            computed_at_utc=NOW,
            breakdown=[
                ModelBaselinesBreakdownRow(
                    id=uuid4(),
                    pattern_id=uuid4(),
                    pattern_label="retry-guard",
                    treatment_success_rate=0.95,
                    sample_count=120,
                    computed_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ],
        )
        result = HANDLER.project(event, db)
        assert "baselines_breakdown" in result.tables_written
        rows = db.query("baselines_breakdown")
        assert len(rows) == 1
        assert rows[0]["pattern_label"] == "retry-guard"

    def test_full_event_all_tables(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesSnapshotEvent(
            snapshot_id=uuid4(),
            computed_at_utc=NOW,
            window_start_utc=NOW,
            window_end_utc=NOW,
            comparisons=[
                ModelBaselinesComparisonRow(
                    id=uuid4(),
                    comparison_date=date(2026, 4, 6),
                    computed_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ],
            trend=[
                ModelBaselinesTrendRow(
                    id=uuid4(),
                    trend_date=date(2026, 4, 6),
                    cohort="control",
                    computed_at=NOW,
                    created_at=NOW,
                ),
            ],
            breakdown=[
                ModelBaselinesBreakdownRow(
                    id=uuid4(),
                    pattern_id=uuid4(),
                    computed_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ],
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 4  # 1 snapshot + 1 each child
        assert len(result.tables_written) == 4

    def test_upsert_snapshot_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        snapshot_id = uuid4()
        HANDLER.project(
            ModelBaselinesSnapshotEvent(
                snapshot_id=snapshot_id,
                computed_at_utc=NOW,
                contract_version=1,
            ),
            db,
        )
        HANDLER.project(
            ModelBaselinesSnapshotEvent(
                snapshot_id=snapshot_id,
                computed_at_utc=NOW,
                contract_version=2,
            ),
            db,
        )
        rows = db.query("baselines_snapshots")
        assert len(rows) == 1
        assert rows[0]["contract_version"] == 2

    def test_event_bus_wiring(self) -> None:
        contract_path = "src/omnimarket/nodes/node_projection_baselines/contract.yaml"
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert (
            "onex.evt.omnibase-infra.baselines-computed.v1"
            in contract["event_bus"]["subscribe_topics"]
        )
        assert (
            contract["terminal_event"]
            == "onex.evt.omnimarket.projection-baselines-applied.v1"
        )
