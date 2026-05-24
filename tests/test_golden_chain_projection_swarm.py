"""Golden chain tests for node_projection_swarm."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import yaml

from omnimarket.nodes.node_projection_swarm.handlers.handler_projection_swarm import (
    HandlerProjectionSwarm,
    ModelSwarmDispatchEvent,
)
from omnimarket.nodes.node_projection_swarm.models.enums import (
    EnumFreshnessState,
    EnumSwarmRunStatus,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionSwarm()

CONTRACT_PATH = "src/omnimarket/nodes/node_projection_swarm/contract.yaml"


def _completed_event(**overrides: object) -> ModelSwarmDispatchEvent:
    defaults: dict[str, object] = {
        "run_id": "run-001",
        "correlation_id": "corr-abc",
        "status": "succeeded",
        "task_hash": "abc123",
        "subtask_count": 4,
        "succeeded_count": 4,
        "failed_count": 0,
        "skipped_count": 0,
        "models_used": ("qwen3-coder-30b", "deepseek-r1-14b"),
        "machines_used": ("192.168.86.201",),
        "total_cost_usd": 0.05,
        "cloud_equivalent_cost_usd": 2.50,
        "savings_usd": 2.45,
        "parallelism_speedup_ratio": 3.2,
        "decomposition_latency_ms": 800,
        "dispatch_wall_latency_ms": 4200,
        "aggregation_latency_ms": 600,
        "total_latency_ms": 5600,
        "emitted_at": datetime.now(UTC).isoformat(),
        "source_topic": "onex.evt.omnimarket.swarm-dispatch-completed.v1",
    }
    defaults.update(overrides)
    return ModelSwarmDispatchEvent(**defaults)


def _failed_event(**overrides: object) -> ModelSwarmDispatchEvent:
    defaults: dict[str, object] = {
        "run_id": "run-fail-001",
        "correlation_id": "corr-fail",
        "status": "failed",
        "task_hash": "xyz789",
        "subtask_count": 3,
        "failed_count": 3,
        "emitted_at": datetime.now(UTC).isoformat(),
        "source_topic": "onex.evt.omnimarket.swarm-dispatch-failed.v1",
    }
    defaults.update(overrides)
    return ModelSwarmDispatchEvent(**defaults)


class TestProjectionSwarmCompletedEvent:
    def test_project_completed_event_upserts_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _completed_event()
        result = HANDLER.project(event, db)

        assert result.rows_upserted == 1
        assert result.run_id == "run-001"
        rows = db.query("swarm_runs")
        assert len(rows) == 1
        assert rows[0]["run_id"] == "run-001"
        assert rows[0]["status"] == "succeeded"
        assert rows[0]["subtask_count"] == 4
        assert rows[0]["succeeded_count"] == 4

    def test_projection_row_cost_fields(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _completed_event()
        HANDLER.project(event, db)
        row = db.query("swarm_runs")[0]
        assert row["total_cost_usd"] == pytest.approx(0.05)
        assert row["cloud_equivalent_cost_usd"] == pytest.approx(2.50)
        assert row["savings_usd"] == pytest.approx(2.45)

    def test_projection_row_latency_fields(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _completed_event()
        HANDLER.project(event, db)
        row = db.query("swarm_runs")[0]
        assert row["total_latency_ms"] == 5600
        assert row["parallelism_speedup_ratio"] == pytest.approx(3.2)

    def test_projection_models_and_machines(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _completed_event()
        HANDLER.project(event, db)
        row = db.query("swarm_runs")[0]
        assert "qwen3-coder-30b" in row["models_used"]
        assert "192.168.86.201" in row["machines_used"]


class TestProjectionSwarmFailedEvent:
    def test_project_failed_event_status(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _failed_event()
        result = HANDLER.project(event, db)

        assert result.rows_upserted == 1
        rows = db.query("swarm_runs")
        assert rows[0]["status"] == "failed"
        assert rows[0]["failed_count"] == 3

    def test_unknown_status_falls_back_to_failed(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _failed_event(status="unknown_garbage")
        HANDLER.project(event, db)
        rows = db.query("swarm_runs")
        assert rows[0]["status"] == EnumSwarmRunStatus.FAILED


class TestProjectionSwarmIdempotency:
    def test_idempotent_upsert_same_run_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        event1 = _completed_event(run_id="run-idempotent", succeeded_count=2)
        event2 = _completed_event(run_id="run-idempotent", succeeded_count=4)
        HANDLER.project(event1, db)
        HANDLER.project(event2, db)

        rows = db.query("swarm_runs")
        assert len(rows) == 1
        assert rows[0]["succeeded_count"] == 4

    def test_different_run_ids_produce_separate_rows(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(_completed_event(run_id="run-a"), db)
        HANDLER.project(_completed_event(run_id="run-b"), db)
        rows = db.query("swarm_runs")
        assert len(rows) == 2


class TestProjectionSwarmFreshness:
    def test_fresh_when_lag_within_sla(self) -> None:
        db = InmemoryDatabaseAdapter()
        recent = datetime.now(UTC).isoformat()
        event = _completed_event(emitted_at=recent)
        result = HANDLER.project(event, db)
        freshness = result.freshness
        assert freshness["freshness_state"] == EnumFreshnessState.FRESH

    def test_stale_when_lag_between_max_and_degraded(self) -> None:
        db = InmemoryDatabaseAdapter()
        stale_ts = (datetime.now(UTC) - timedelta(seconds=45)).isoformat()
        event = _completed_event(emitted_at=stale_ts)
        result = HANDLER.project(event, db)
        assert result.freshness["freshness_state"] == EnumFreshnessState.STALE

    def test_degraded_when_lag_exceeds_degraded_threshold(self) -> None:
        db = InmemoryDatabaseAdapter()
        old_ts = (datetime.now(UTC) - timedelta(seconds=90)).isoformat()
        event = _completed_event(emitted_at=old_ts)
        result = HANDLER.project(event, db)
        assert result.freshness["freshness_state"] == EnumFreshnessState.DEGRADED

    def test_degraded_when_emitted_at_missing(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _completed_event(emitted_at=None)
        result = HANDLER.project(event, db)
        freshness = result.freshness
        assert freshness["freshness_state"] == EnumFreshnessState.DEGRADED
        assert "emitted_at missing" in freshness["degraded_reason"]

    def test_degraded_when_emitted_at_unparseable(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _completed_event(emitted_at="not-a-timestamp")
        result = HANDLER.project(event, db)
        assert result.freshness["freshness_state"] == EnumFreshnessState.DEGRADED


class TestProjectionSwarmAppliedEvent:
    def test_applied_event_contains_projection_and_freshness(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = _completed_event()
        result = HANDLER.project(event, db)

        assert result.run_id == "run-001"
        assert result.table == "swarm_runs"
        assert "run_id" in result.projection
        assert "freshness_state" in result.freshness
        assert result.applied_at is not None

    def test_handle_dict_interface(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "_db": db,
            "run_id": "run-handle-001",
            "correlation_id": "corr-handle",
            "status": "succeeded",
            "task_hash": "h1",
            "subtask_count": 1,
            "emitted_at": datetime.now(UTC).isoformat(),
            "source_topic": "onex.evt.omnimarket.swarm-dispatch-completed.v1",
        }
        result = HANDLER.handle(payload)
        assert result["run_id"] == "run-handle-001"
        assert result["rows_upserted"] == 1

    def test_handle_raises_without_db_adapter(self) -> None:
        with pytest.raises(TypeError, match="DatabaseAdapter"):
            HANDLER.handle({"run_id": "x", "status": "succeeded", "task_hash": "h"})


class TestProjectionSwarmBatch:
    def test_project_batch(self) -> None:
        db = InmemoryDatabaseAdapter()
        events = [_completed_event(run_id=f"run-{i:03d}") for i in range(5)]
        results = HANDLER.project_batch(events, db)
        assert len(results) == 5
        assert all(r.rows_upserted == 1 for r in results)
        assert len(db.query("swarm_runs")) == 5


class TestProjectionSwarmContractWiring:
    def test_subscribe_topics_in_contract(self) -> None:
        with open(CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        subs = contract["event_bus"]["subscribe_topics"]
        assert "onex.evt.omnimarket.swarm-dispatch-completed.v1" in subs
        assert "onex.evt.omnimarket.swarm-dispatch-failed.v1" in subs

    def test_publish_topic_in_contract(self) -> None:
        with open(CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        pubs = contract["event_bus"]["publish_topics"]
        assert "onex.evt.omnimarket.projection-swarm-applied.v1" in pubs

    def test_terminal_event_in_contract(self) -> None:
        with open(CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        assert (
            contract["terminal_event"]
            == "onex.evt.omnimarket.projection-swarm-applied.v1"
        )

    def test_freshness_sla_in_contract(self) -> None:
        with open(CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        sla = contract["freshness_sla"]
        assert sla["max_lag_seconds"] == 30
        assert sla["degraded_after_seconds"] == 60


class TestProjectionSwarmModels:
    def test_model_swarm_run_projection_frozen(self) -> None:
        from omnimarket.nodes.node_projection_swarm.models.model_swarm_run_projection import (
            ModelSwarmRunProjection,
        )

        row = ModelSwarmRunProjection(
            run_id="r1",
            correlation_id="c1",
            status=EnumSwarmRunStatus.SUCCEEDED,
            task_hash="h1",
            subtask_count=2,
            created_at="2026-05-24T00:00:00Z",
        )
        with pytest.raises(ValueError, match="frozen"):
            row.run_id = "other"  # type: ignore[misc]

    def test_model_projection_freshness_frozen(self) -> None:
        from omnimarket.nodes.node_projection_swarm.models.model_projection_freshness import (
            ModelProjectionFreshness,
        )

        f = ModelProjectionFreshness(
            freshness_state=EnumFreshnessState.FRESH,
            observed_at="2026-05-24T00:00:00Z",
        )
        with pytest.raises(ValueError, match="frozen"):
            f.freshness_state = EnumFreshnessState.STALE  # type: ignore[misc]

    def test_enum_values(self) -> None:
        assert EnumSwarmRunStatus.SUCCEEDED == "succeeded"
        assert EnumSwarmRunStatus.FAILED == "failed"
        assert EnumFreshnessState.FRESH == "fresh"
        assert EnumFreshnessState.DEGRADED == "degraded"
