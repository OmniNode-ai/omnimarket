# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for node_projection_baselines_quality — NC-03.

Verifies that HandlerProjectionBaselinesQuality projects a
ModelBaselinesSnapshotEvent (the real producer contract) into the
baselines_quality_snapshots table with the correct quality summary fields.

Coverage:
- Snapshot row is upserted with correct confidence tier counts (derived
  from breakdown.confidence, the real sample-sufficiency proxy)
- quality_score is computed as the mean of comparison.treatment_success_rate
- significant_rate is computed from patterns_significant / patterns_compared
- Re-projection of the same snapshot_id upserts (not inserts) the row
- Empty comparisons/breakdown produce zero-value rows
- Contract declares the correct subscribe topic and projection_api snapshot topic
- handle() shim delegates to project()
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

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

from omnimarket.nodes.node_projection_baselines_quality.handlers.handler_projection_baselines_quality import (
    HandlerProjectionBaselinesQuality,
    ModelBaselinesQualityProjectionResult,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_CONTRACT_PATH = "src/omnimarket/nodes/node_projection_baselines_quality/contract.yaml"

HANDLER = HandlerProjectionBaselinesQuality()
TABLE = "baselines_quality_snapshots"
_NOW = datetime(2026, 6, 28, 10, 0, 0, tzinfo=UTC)
_DEFAULT_SNAPSHOT_ID = UUID("11111111-2222-3333-4444-555555555555")


def _comparison(
    comp_id: str, treatment_success_rate: float | None
) -> ModelBaselinesComparisonRow:
    return ModelBaselinesComparisonRow(
        id=UUID(comp_id),
        comparison_date=date(2026, 6, 28),
        treatment_success_rate=treatment_success_rate,
        computed_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _breakdown(
    bd_id: str, pattern_id: str, confidence: float | None
) -> ModelBaselinesBreakdownRow:
    return ModelBaselinesBreakdownRow(
        id=UUID(bd_id),
        pattern_id=UUID(pattern_id),
        confidence=confidence,
        sample_count=25 if confidence is not None else 5,
        computed_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _full_event(
    snapshot_id: UUID = _DEFAULT_SNAPSHOT_ID,
) -> ModelBaselinesSnapshotEvent:
    return ModelBaselinesSnapshotEvent(
        snapshot_id=snapshot_id,
        computed_at_utc=_NOW,
        comparisons=[
            _comparison("aaaaaaaa-0000-0000-0000-000000000001", 0.9),
            _comparison("aaaaaaaa-0000-0000-0000-000000000002", 0.5),
        ],
        breakdown=[
            _breakdown(
                "bbbbbbbb-0000-0000-0000-000000000001",
                "cccccccc-0000-0000-0000-000000000001",
                0.9,
            ),
            _breakdown(
                "bbbbbbbb-0000-0000-0000-000000000002",
                "cccccccc-0000-0000-0000-000000000002",
                0.6,
            ),
            _breakdown(
                "bbbbbbbb-0000-0000-0000-000000000003",
                "cccccccc-0000-0000-0000-000000000003",
                None,
            ),
            _breakdown(
                "bbbbbbbb-0000-0000-0000-000000000004",
                "cccccccc-0000-0000-0000-000000000004",
                0.85,
            ),
        ],
    )


class TestHandlerProjectionBaselinesQuality:
    def test_project_writes_quality_snapshot_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        result = HANDLER.project(event, db)

        assert isinstance(result, ModelBaselinesQualityProjectionResult)
        assert result.rows_upserted == 1
        assert result.table == TABLE

        rows = db.query(TABLE)
        assert len(rows) == 1
        row = rows[0]

        assert row["snapshot_id"] == "11111111-2222-3333-4444-555555555555"
        assert row["captured_at"] == _NOW.isoformat()

    def test_confidence_tier_counts_are_correct(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        # 2 high (0.9, 0.85), 1 medium (0.6), 1 low (None -> insufficient sample)
        assert row["high_confidence_count"] == 2
        assert row["medium_confidence_count"] == 1
        assert row["low_confidence_count"] == 1

    def test_quality_score_is_mean_treatment_success_rate(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        # (0.9 + 0.5) / 2 = 0.7
        assert abs(float(str(row["quality_score"])) - 0.7) < 0.0001

    def test_significant_rate_is_ratio(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        # 3 of 4 breakdown rows have non-null confidence -> 0.75
        assert abs(float(str(row["significant_rate"])) - 0.75) < 0.0001

    def test_patterns_counts_are_written(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        assert row["patterns_compared"] == 4
        assert row["patterns_significant"] == 3

    def test_empty_event_produces_zero_values(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesSnapshotEvent(
            snapshot_id=UUID("00000000-0000-0000-0000-000000000000"),
            computed_at_utc=_NOW,
        )
        result = HANDLER.project(event, db)

        assert result.rows_upserted == 1
        row = db.query(TABLE)[0]
        assert float(str(row["quality_score"])) == 0.0
        assert float(str(row["significant_rate"])) == 0.0
        assert row["high_confidence_count"] == 0
        assert row["medium_confidence_count"] == 0
        assert row["low_confidence_count"] == 0

    def test_upsert_on_duplicate_snapshot_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        snap_id = UUID("99999999-0000-0000-0000-000000000000")
        ev1 = ModelBaselinesSnapshotEvent(
            snapshot_id=snap_id,
            computed_at_utc=_NOW,
            breakdown=[
                _breakdown(
                    "dddddddd-0000-0000-0000-000000000001",
                    "eeeeeeee-0000-0000-0000-000000000001",
                    0.9,
                ),
            ],
        )
        ev2 = ModelBaselinesSnapshotEvent(
            snapshot_id=snap_id,
            computed_at_utc=_NOW,
            breakdown=[
                _breakdown(
                    "dddddddd-0000-0000-0000-000000000002",
                    "eeeeeeee-0000-0000-0000-000000000002",
                    None,
                ),
                _breakdown(
                    "dddddddd-0000-0000-0000-000000000003",
                    "eeeeeeee-0000-0000-0000-000000000003",
                    None,
                ),
            ],
        )
        HANDLER.project(ev1, db)
        HANDLER.project(ev2, db)

        rows = db.query(TABLE)
        assert len(rows) == 1
        # Second event should dominate
        assert rows[0]["high_confidence_count"] == 0
        assert rows[0]["low_confidence_count"] == 2

    def test_contract_subscribe_topic(self) -> None:
        with open(_CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        subscribe = contract["event_bus"]["subscribe_topics"]
        assert "onex.evt.omnibase-infra.baselines-computed.v1" in subscribe

    def test_contract_projection_api_snapshot_topic(self) -> None:
        with open(_CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        exposures = contract["projection_api"]["exposures"]
        topics = [e["topic"] for e in exposures]
        assert "onex.snapshot.projection.baselines.quality.v1" in topics

    def test_contract_node_type_is_reducer(self) -> None:
        with open(_CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        assert contract["node_type"] == "reducer"

    def test_contract_dlq_topic(self) -> None:
        with open(_CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        assert (
            "onex.dlq.omnimarket.projection-baselines-quality-malformed.v1"
            in contract["event_bus"]["dlq_topics"]
        )

    def test_handle_shim_delegates_to_project(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event(UUID("22222222-3333-4444-5555-666666666666"))
        payload = event.model_dump(mode="json")
        payload["_db"] = db

        out = HANDLER.handle(payload)
        assert out["rows_upserted"] == 1
        assert out["table"] == TABLE
