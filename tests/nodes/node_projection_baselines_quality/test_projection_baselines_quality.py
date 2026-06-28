# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for node_projection_baselines_quality — NC-03.

Verifies that HandlerProjectionBaselinesQuality projects a
ModelBaselinesComputedEvent into the baselines_quality_snapshots table with
the correct quality summary fields.

Coverage:
- Snapshot row is upserted with correct confidence tier counts
- quality_score is computed as a weighted average of confidence tiers
- recommend_rate is computed from patterns_recommended / patterns_compared
- Re-projection of the same snapshot_id upserts (not inserts) the row
- Empty comparisons produce zero quality_score; zero patterns_compared gives 0.0 recommend_rate
- Contract declares the correct subscribe topic and projection_api snapshot topic
- handle() shim delegates to project()
"""

from __future__ import annotations

import yaml

from omnimarket.nodes.node_projection_baselines.handlers.handler_projection_baselines import (
    ModelBaselinesComparison,
    ModelBaselinesComputedEvent,
    ModelBaselinesRecommendation,
    ModelBaselinesRetryCount,
)
from omnimarket.nodes.node_projection_baselines_quality.handlers.handler_projection_baselines_quality import (
    HandlerProjectionBaselinesQuality,
    ModelBaselinesQualityProjectionResult,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_CONTRACT_PATH = "src/omnimarket/nodes/node_projection_baselines_quality/contract.yaml"

HANDLER = HandlerProjectionBaselinesQuality()
TABLE = "baselines_quality_snapshots"


def _full_event(snapshot_id: str = "snap-quality-001") -> ModelBaselinesComputedEvent:
    return ModelBaselinesComputedEvent(
        snapshot_id=snapshot_id,
        computed_at_utc="2026-06-28T10:00:00Z",
        patterns_compared=4,
        patterns_recommended=2,
        comparisons=[
            ModelBaselinesComparison(
                pattern_id="pat-a",
                token_delta=-1000,
                time_delta_s=0.5,
                confidence="high",
            ),
            ModelBaselinesComparison(
                pattern_id="pat-b",
                token_delta=200,
                time_delta_s=-0.1,
                confidence="medium",
            ),
            ModelBaselinesComparison(
                pattern_id="pat-c",
                token_delta=0,
                time_delta_s=0.0,
                confidence="low",
            ),
            ModelBaselinesComparison(
                pattern_id="pat-d",
                token_delta=100,
                time_delta_s=0.2,
                confidence="high",
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
                pattern_id="pat-c",
                retry_count=2,
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

        assert row["snapshot_id"] == "snap-quality-001"
        assert row["captured_at"] == "2026-06-28T10:00:00Z"

    def test_confidence_tier_counts_are_correct(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        # 2 high (pat-a, pat-d), 1 medium (pat-b), 1 low (pat-c)
        assert row["high_confidence_count"] == 2
        assert row["medium_confidence_count"] == 1
        assert row["low_confidence_count"] == 1

    def test_quality_score_is_weighted_average(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        # (2*1.0 + 1*0.5 + 1*0.25) / 4 = 2.75 / 4 = 0.6875
        assert abs(float(str(row["quality_score"])) - 0.6875) < 0.0001

    def test_recommend_rate_is_ratio(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        # 2 recommended / 4 compared = 0.5
        assert abs(float(str(row["recommend_rate"])) - 0.5) < 0.0001

    def test_patterns_counts_are_written(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event()
        HANDLER.project(event, db)

        row = db.query(TABLE)[0]
        assert row["patterns_compared"] == 4
        assert row["patterns_recommended"] == 2

    def test_empty_comparisons_produces_zero_quality_score(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelBaselinesComputedEvent(
            snapshot_id="snap-empty",
            computed_at_utc="2026-06-28T00:00:00Z",
        )
        result = HANDLER.project(event, db)

        assert result.rows_upserted == 1
        row = db.query(TABLE)[0]
        assert float(str(row["quality_score"])) == 0.0
        assert float(str(row["recommend_rate"])) == 0.0
        assert row["high_confidence_count"] == 0
        assert row["medium_confidence_count"] == 0
        assert row["low_confidence_count"] == 0

    def test_upsert_on_duplicate_snapshot_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        ev1 = ModelBaselinesComputedEvent(
            snapshot_id="snap-dup",
            computed_at_utc="2026-06-28T08:00:00Z",
            patterns_compared=2,
            patterns_recommended=1,
            comparisons=[
                ModelBaselinesComparison(pattern_id="p1", confidence="high"),
            ],
        )
        ev2 = ModelBaselinesComputedEvent(
            snapshot_id="snap-dup",
            computed_at_utc="2026-06-28T09:00:00Z",
            patterns_compared=3,
            patterns_recommended=3,
            comparisons=[
                ModelBaselinesComparison(pattern_id="p2", confidence="low"),
                ModelBaselinesComparison(pattern_id="p3", confidence="low"),
                ModelBaselinesComparison(pattern_id="p4", confidence="low"),
            ],
        )
        HANDLER.project(ev1, db)
        HANDLER.project(ev2, db)

        rows = db.query(TABLE)
        assert len(rows) == 1
        # Second event should dominate
        assert rows[0]["high_confidence_count"] == 0
        assert rows[0]["low_confidence_count"] == 3

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

    def test_handle_shim_delegates_to_project(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _full_event("snap-shim")
        payload = event.model_dump(mode="json")
        payload["_db"] = db

        out = HANDLER.handle(payload)
        assert out["rows_upserted"] == 1
        assert out["table"] == TABLE
