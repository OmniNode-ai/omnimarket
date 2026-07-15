# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for node_projection_baselines_roi — NC-02.

Verifies that HandlerProjectionBaselinesRoi projects a
ModelBaselinesSnapshotEvent (the real producer contract) into the
baselines_roi_snapshots table with the correct ROI summary fields.

Coverage:
- Snapshot row is upserted with correct token_delta, roi_pct_avg,
  latency_improvement_pct_avg, cost_improvement_pct_avg, sample_size
- Re-projection of the same snapshot_id upserts (not inserts) the row
- Empty comparisons produce zero-value rows
- Contract declares the correct subscribe topic and projection_api snapshot topic
- handle() shim delegates to project()
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

import yaml
from omnibase_infra.services.observability.baselines.models.model_baselines_comparison_row import (
    ModelBaselinesComparisonRow,
)
from omnibase_infra.services.observability.baselines.models.model_baselines_snapshot_event import (
    ModelBaselinesSnapshotEvent,
)

from omnimarket.nodes.node_projection_baselines_roi.handlers.handler_projection_baselines_roi import (
    HandlerProjectionBaselinesRoi,
    ModelBaselinesRoiProjectionResult,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_CONTRACT_PATH = "src/omnimarket/nodes/node_projection_baselines_roi/contract.yaml"

HANDLER = HandlerProjectionBaselinesRoi()
TABLE = "baselines_roi_snapshots"
_NOW = datetime(2026, 6, 28, 10, 0, 0, tzinfo=UTC)
_DEFAULT_SNAPSHOT_ID = UUID("11111111-2222-3333-4444-555555555555")


def _comparison(
    comp_id: str,
    *,
    treatment_total_tokens: int = 0,
    control_total_tokens: int = 0,
    roi_pct: float | None = None,
    latency_improvement_pct: float | None = None,
    cost_improvement_pct: float | None = None,
    sample_size: int = 0,
) -> ModelBaselinesComparisonRow:
    return ModelBaselinesComparisonRow(
        id=UUID(comp_id),
        comparison_date=date(2026, 6, 28),
        treatment_total_tokens=treatment_total_tokens,
        control_total_tokens=control_total_tokens,
        roi_pct=roi_pct,
        latency_improvement_pct=latency_improvement_pct,
        cost_improvement_pct=cost_improvement_pct,
        sample_size=sample_size,
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
            _comparison(
                "aaaaaaaa-0000-0000-0000-000000000001",
                treatment_total_tokens=6000,
                control_total_tokens=8000,
                roi_pct=25.0,
                latency_improvement_pct=39.3,
                cost_improvement_pct=25.0,
                sample_size=120,
            ),
            _comparison(
                "aaaaaaaa-0000-0000-0000-000000000002",
                treatment_total_tokens=4000,
                control_total_tokens=4500,
                roi_pct=None,
                latency_improvement_pct=None,
                cost_improvement_pct=11.1,
                sample_size=80,
            ),
        ],
    )


class TestHandlerProjectionBaselinesRoi:
    def test_project_writes_roi_snapshot_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        result = HANDLER.project(event, db)

        assert isinstance(result, ModelBaselinesRoiProjectionResult)
        assert result.rows_upserted == 1
        assert result.table == TABLE

        rows = db.query(TABLE)
        assert len(rows) == 1
        row = rows[0]

        assert row["snapshot_id"] == "11111111-2222-3333-4444-555555555555"
        assert row["captured_at"] == _NOW.isoformat()

    def test_token_delta_is_control_minus_treatment_tokens(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        # control: 8000 + 4500 = 12500; treatment: 6000 + 4000 = 10000
        assert row["token_delta"] == 2500

    def test_roi_pct_avg_ignores_nulls(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        # only one comparison has a non-null roi_pct: 25.0
        assert abs(float(str(row["roi_pct_avg"])) - 25.0) < 0.001

    def test_cost_improvement_pct_avg_is_mean(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        assert abs(float(str(row["cost_improvement_pct_avg"])) - 18.05) < 0.001

    def test_sample_size_is_sum(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        assert row["sample_size"] == 200

    def test_empty_event_produces_zero_values(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesSnapshotEvent(
            snapshot_id=UUID("00000000-0000-0000-0000-000000000000"),
            computed_at_utc=_NOW,
        )
        result = HANDLER.project(event, db)

        assert result.rows_upserted == 1
        row = db.query(TABLE)[0]
        assert row["token_delta"] == 0
        assert float(str(row["roi_pct_avg"])) == 0.0
        assert float(str(row["latency_improvement_pct_avg"])) == 0.0
        assert float(str(row["cost_improvement_pct_avg"])) == 0.0
        assert row["sample_size"] == 0

    def test_upsert_on_duplicate_snapshot_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        snap_id = UUID("99999999-0000-0000-0000-000000000000")
        ev1 = ModelBaselinesSnapshotEvent(
            snapshot_id=snap_id,
            computed_at_utc=_NOW,
            comparisons=[
                _comparison(
                    "bbbbbbbb-0000-0000-0000-000000000001",
                    treatment_total_tokens=100,
                    control_total_tokens=100,
                )
            ],
        )
        ev2 = ModelBaselinesSnapshotEvent(
            snapshot_id=snap_id,
            computed_at_utc=_NOW,
            comparisons=[
                _comparison(
                    "bbbbbbbb-0000-0000-0000-000000000002",
                    treatment_total_tokens=100,
                    control_total_tokens=300,
                )
            ],
        )
        HANDLER.project(ev1, db)
        HANDLER.project(ev2, db)

        rows = db.query(TABLE)
        assert len(rows) == 1
        assert rows[0]["token_delta"] == 200

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
        assert "onex.snapshot.projection.baselines.roi.v1" in topics

    def test_contract_terminal_event(self) -> None:
        with open(_CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        assert (
            contract["terminal_event"]
            == "onex.evt.omnimarket.projection-baselines-roi-applied.v1"
        )

    def test_contract_dlq_topic(self) -> None:
        with open(_CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        assert (
            "onex.dlq.omnimarket.projection-baselines-roi-malformed.v1"
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

    def test_handle_shim_tolerates_transport_keys(self) -> None:
        """extra='forbid' on the producer model must tolerate injected keys."""
        db = InmemoryDatabaseAdapter()
        event = _full_event(UUID("33333333-4444-5555-6666-777777777777"))
        payload = event.model_dump(mode="json")
        payload["_db"] = db
        payload["_envelope"] = {"payload": {}}
        payload["_event_type"] = "onex.evt.omnibase-infra.baselines-computed.v1"
        payload["_correlation_id"] = "corr-1"

        out = HANDLER.handle(payload)
        assert out["rows_upserted"] == 1
