# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for node_projection_baselines_roi — NC-02.

Verifies that HandlerProjectionBaselinesRoi projects a
ModelBaselinesComputedEvent into the baselines_roi_snapshots table with the
correct ROI summary fields.

Coverage:
- Snapshot row is upserted with correct token_delta, time_delta_ms, retry_delta
- recommendations JSONB column counts actions correctly
- confidence is computed as an average of mapped scores
- Re-projection of the same snapshot_id upserts (not inserts) the row
- Empty comparisons / retry_counts / recommendations produce zero-value rows
- Contract declares the correct subscribe topic and projection_api snapshot topic
"""

from __future__ import annotations

import yaml

from omnimarket.nodes.node_projection_baselines.handlers.handler_projection_baselines import (
    ModelBaselinesComparison,
    ModelBaselinesComputedEvent,
    ModelBaselinesRecommendation,
    ModelBaselinesRetryCount,
)
from omnimarket.nodes.node_projection_baselines_roi.handlers.handler_projection_baselines_roi import (
    HandlerProjectionBaselinesRoi,
    ModelBaselinesRoiProjectionResult,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_CONTRACT_PATH = "src/omnimarket/nodes/node_projection_baselines_roi/contract.yaml"

HANDLER = HandlerProjectionBaselinesRoi()
TABLE = "baselines_roi_snapshots"


def _full_event(snapshot_id: str = "snap-roi-001") -> ModelBaselinesComputedEvent:
    return ModelBaselinesComputedEvent(
        snapshot_id=snapshot_id,
        computed_at_utc="2026-06-28T10:00:00Z",
        patterns_compared=3,
        patterns_recommended=2,
        comparisons=[
            ModelBaselinesComparison(
                pattern_id="pat-a",
                token_delta=-2000,
                time_delta_s=1.5,
                confidence="high",
            ),
            ModelBaselinesComparison(
                pattern_id="pat-b",
                token_delta=500,
                time_delta_s=-0.3,
                confidence="medium",
            ),
        ],
        recommendations=[
            ModelBaselinesRecommendation(
                pattern_id="pat-a",
                action="promote",
                confidence="high",
            ),
            ModelBaselinesRecommendation(
                pattern_id="pat-b",
                action="shadow",
                confidence="medium",
            ),
        ],
        retry_counts=[
            ModelBaselinesRetryCount(
                pattern_id="pat-a",
                retry_count=3,
            ),
            ModelBaselinesRetryCount(
                pattern_id="pat-b",
                retry_count=1,
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

        assert row["snapshot_id"] == "snap-roi-001"
        assert row["captured_at"] == "2026-06-28T10:00:00Z"

    def test_token_delta_is_sum_of_comparisons(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        # -2000 + 500 = -1500
        assert row["token_delta"] == -1500

    def test_time_delta_ms_is_sum_converted(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        # (1.5 + -0.3) * 1000 = 1200ms
        assert abs(float(str(row["time_delta_ms"])) - 1200.0) < 0.01

    def test_retry_delta_is_sum_of_retry_counts(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        # 3 + 1 = 4
        assert row["retry_delta"] == 4

    def test_recommendations_counts_actions(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        recs = row["recommendations"]
        assert isinstance(recs, dict)
        assert recs["promote"] == 1
        assert recs["shadow"] == 1
        assert recs["suppress"] == 0
        assert recs["fork"] == 0

    def test_confidence_is_average_of_mapped_scores(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        # comparisons: high=1.0, medium=0.5 → average = 0.75
        assert abs(float(str(row["confidence"])) - 0.75) < 0.001

    def test_empty_event_produces_zero_values(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesComputedEvent(
            snapshot_id="snap-empty",
            computed_at_utc="2026-06-28T00:00:00Z",
        )
        result = HANDLER.project(event, db)

        assert result.rows_upserted == 1
        row = db.query(TABLE)[0]
        assert row["token_delta"] == 0
        assert row["retry_delta"] == 0
        assert float(str(row["time_delta_ms"])) == 0.0
        recs = row["recommendations"]
        assert all(recs[k] == 0 for k in ("promote", "shadow", "suppress", "fork"))
        assert float(str(row["confidence"])) == 0.0

    def test_upsert_on_duplicate_snapshot_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        ev1 = ModelBaselinesComputedEvent(
            snapshot_id="snap-dup",
            computed_at_utc="2026-06-28T08:00:00Z",
            comparisons=[ModelBaselinesComparison(pattern_id="p1", token_delta=100)],
        )
        ev2 = ModelBaselinesComputedEvent(
            snapshot_id="snap-dup",
            computed_at_utc="2026-06-28T09:00:00Z",
            comparisons=[ModelBaselinesComparison(pattern_id="p2", token_delta=200)],
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

    def test_handle_shim_delegates_to_project(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event("snap-shim")
        payload = event.model_dump(mode="json")
        payload["_db"] = db

        out = HANDLER.handle(payload)
        assert out["rows_upserted"] == 1
        assert out["table"] == TABLE
