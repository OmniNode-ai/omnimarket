"""Golden chain tests for node_projection_query (OMN-8900).

Tests all 10 query shapes against InmemoryDatabaseAdapter with pre-seeded data.
"""

from __future__ import annotations

import pytest
import yaml

from omnimarket.nodes.node_projection_query.handlers.handler_projection_query import (
    SUPPORTED_SHAPES,
    HandlerProjectionQuery,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionQuery()

SHAPES = [
    "staleness",
    "projection-health",
    "system-activity",
    "hot-nodes",
    "intent-drift",
    "contract-drift",
    "model-efficiency",
    "dlq",
    "eval-results",
    "intent-breakdown",
]


def _seed_staleness(db: InmemoryDatabaseAdapter) -> None:
    db.upsert(
        "pattern_learning_artifacts",
        "id",
        {
            "id": "pla-1",
            "projected_at": "2026-04-15T10:00:00Z",
        },
    )
    db.upsert(
        "injection_effectiveness",
        "id",
        {
            "id": "ie-1",
            "created_at": "2026-04-15T09:00:00Z",
        },
    )


def _seed_projection_health(db: InmemoryDatabaseAdapter) -> None:
    db.upsert(
        "agent_routing_decisions",
        "id",
        {
            "id": "ard-1",
            "created_at": "2026-04-15T10:00:00Z",
        },
    )
    db.upsert(
        "agent_routing_decisions",
        "id",
        {
            "id": "ard-2",
            "created_at": "2026-04-15T10:05:00Z",
        },
    )
    db.upsert(
        "projection_watermarks",
        "projection_name",
        {
            "projection_name": "agent-actions",
            "last_offset": 42,
            "events_projected": 100,
            "errors_count": 0,
        },
    )


def _seed_system_activity(db: InmemoryDatabaseAdapter) -> None:
    db.upsert(
        "phase_metrics_events",
        "id",
        {
            "id": "pme-1",
            "session_id": "s1",
            "phase": "BUILDING",
            "status": "active",
            "duration_ms": 5000,
            "emitted_at": "2026-04-15T10:00:00Z",
        },
    )
    db.upsert(
        "skill_invocations",
        "id",
        {
            "id": "si-1",
            "skill_name": "ticket_work",
            "session_id": "s1",
            "duration_ms": 12000,
            "success": True,
            "created_at": "2026-04-15T10:00:00Z",
        },
    )
    db.upsert(
        "session_outcomes",
        "session_id",
        {
            "session_id": "s1",
            "outcome": "success",
            "emitted_at": "2026-04-15T10:00:00Z",
        },
    )
    db.upsert(
        "delegation_events",
        "id",
        {
            "id": "de-1",
            "task_type": "build",
            "delegated_to": "sonnet",
            "quality_gate_passed": True,
            "cost_usd": 0.05,
            "timestamp": "2026-04-15T10:00:00Z",
        },
    )


def _seed_hot_nodes(db: InmemoryDatabaseAdapter) -> None:
    for i in range(5):
        db.upsert(
            "agent_routing_decisions",
            "id",
            {
                "id": f"ard-hot-{i}",
                "selected_agent": "node_ticket_pipeline"
                if i < 3
                else "node_aislop_sweep",
                "created_at": "2026-04-15T10:00:00Z",
            },
        )


def _seed_intent_drift(db: InmemoryDatabaseAdapter) -> None:
    db.upsert(
        "intent_drift_events",
        "id",
        {
            "id": "ide-1",
            "session_id": "s1",
            "original_intent": "debug",
            "current_intent": "refactor",
            "drift_score": 0.8,
            "severity": "high",
            "created_at": "2026-04-15T10:00:00Z",
        },
    )


def _seed_contract_drift(db: InmemoryDatabaseAdapter) -> None:
    db.upsert(
        "contract_drift_events",
        "id",
        {
            "id": "cde-1",
            "repo": "omnibase_core",
            "node_name": "node_foo",
            "drift_type": "missing_handler",
            "severity": "critical",
            "description": "Handler not wired",
            "detected_at": "2026-04-15T10:00:00Z",
            "created_at": "2026-04-15T10:00:00Z",
        },
    )


def _seed_model_efficiency(db: InmemoryDatabaseAdapter) -> None:
    db.upsert(
        "model_efficiency_rollups",
        "run_id",
        {
            "run_id": "run-1",
            "model_id": "claude-sonnet-4-20250514",
            "rollup_status": "final",
            "vts": 0.85,
            "vts_per_kloc": 0.42,
            "blocking_failures": 0,
            "reruns": 1,
            "autofix_successes": 1,
            "time_to_green_ms": 45000,
            "emitted_at": "2026-04-15T10:00:00Z",
        },
    )
    db.upsert(
        "model_efficiency_rollups",
        "run_id",
        {
            "run_id": "run-2",
            "model_id": "claude-sonnet-4-20250514",
            "rollup_status": "partial",
            "vts": 0.5,
            "vts_per_kloc": 0.3,
            "blocking_failures": 1,
            "reruns": 0,
            "autofix_successes": 0,
            "time_to_green_ms": 0,
            "emitted_at": "2026-04-15T09:00:00Z",
        },
    )


def _seed_dlq(db: InmemoryDatabaseAdapter) -> None:
    db.upsert(
        "dlq_messages",
        "id",
        {
            "id": "dlq-1",
            "original_topic": "onex.evt.test.v1",
            "error_message": "Schema validation failed",
            "error_type": "ValidationError",
            "retry_count": 3,
            "consumer_group": "test-group",
            "created_at": "2026-04-15T10:00:00Z",
        },
    )


def _seed_eval_results(db: InmemoryDatabaseAdapter) -> None:
    db.upsert(
        "eval_reports",
        "report_id",
        {
            "report_id": "eval-1",
            "suite_id": "core-v1",
            "suite_version": "1.0.0",
            "generated_at": "2026-04-15T10:00:00Z",
            "total_tasks": 50,
            "onex_better_count": 35,
            "onex_worse_count": 5,
            "neutral_count": 10,
        },
    )


def _seed_intent_breakdown(db: InmemoryDatabaseAdapter) -> None:
    db.upsert(
        "intent_signals",
        "id",
        {
            "id": "is-1",
            "intent_type": "debug",
            "created_at": "2026-04-15T10:00:00Z",
        },
    )
    db.upsert(
        "intent_signals",
        "id",
        {
            "id": "is-2",
            "intent_type": "debug",
            "created_at": "2026-04-15T10:01:00Z",
        },
    )
    db.upsert(
        "intent_signals",
        "id",
        {
            "id": "is-3",
            "intent_type": "refactor",
            "created_at": "2026-04-15T10:02:00Z",
        },
    )


class TestProjectionQueryAllShapes:
    """Every supported shape is enumerated and tested."""

    def test_supported_shapes_match_spec(self) -> None:
        assert set(SUPPORTED_SHAPES) == set(SHAPES)

    @pytest.mark.parametrize("shape", SHAPES)
    def test_shape_returns_data_key(self, shape: str) -> None:
        db = InmemoryDatabaseAdapter()
        result = HANDLER.query(shape, {}, db)
        assert "shape" in result
        assert result["shape"] == shape
        assert "data" in result
        assert "queried_at" in result

    def test_unknown_shape_raises(self) -> None:
        db = InmemoryDatabaseAdapter()
        with pytest.raises(ValueError, match="Unsupported query shape"):
            HANDLER.query("nonexistent-shape", {}, db)


class TestStaleness:
    def test_returns_feature_timestamps(self) -> None:
        db = InmemoryDatabaseAdapter()
        _seed_staleness(db)
        result = HANDLER.query("staleness", {}, db)
        features = result["data"]["features"]
        assert isinstance(features, dict)
        assert len(features) > 0


class TestProjectionHealth:
    def test_returns_table_counts_and_watermarks(self) -> None:
        db = InmemoryDatabaseAdapter()
        _seed_projection_health(db)
        result = HANDLER.query("projection-health", {}, db)
        data = result["data"]
        assert "tables" in data
        assert "watermarks" in data
        assert "summary" in data
        assert data["summary"]["populated_tables"] > 0


class TestSystemActivity:
    def test_returns_all_subsections(self) -> None:
        db = InmemoryDatabaseAdapter()
        _seed_system_activity(db)
        result = HANDLER.query("system-activity", {}, db)
        data = result["data"]
        assert "build_loop" in data
        assert "pipelines" in data
        assert "sessions" in data
        assert "delegations" in data

    def test_build_loop_has_phases(self) -> None:
        db = InmemoryDatabaseAdapter()
        _seed_system_activity(db)
        result = HANDLER.query("system-activity", {}, db)
        assert len(result["data"]["build_loop"]) > 0


class TestHotNodes:
    def test_returns_ranked_nodes(self) -> None:
        db = InmemoryDatabaseAdapter()
        _seed_hot_nodes(db)
        result = HANDLER.query("hot-nodes", {}, db)
        nodes = result["data"]["nodes"]
        assert isinstance(nodes, list)
        assert len(nodes) > 0
        assert "node_id" in nodes[0]
        assert "event_count" in nodes[0]


class TestIntentDrift:
    def test_returns_recent_and_summary(self) -> None:
        db = InmemoryDatabaseAdapter()
        _seed_intent_drift(db)
        result = HANDLER.query("intent-drift", {}, db)
        data = result["data"]
        assert "recent" in data
        assert "summary" in data
        assert len(data["recent"]) == 1
        assert data["recent"][0]["severity"] == "high"


class TestContractDrift:
    def test_returns_events_and_breakdowns(self) -> None:
        db = InmemoryDatabaseAdapter()
        _seed_contract_drift(db)
        result = HANDLER.query("contract-drift", {}, db)
        data = result["data"]
        assert "recent" in data
        assert "by_severity" in data
        assert "by_type" in data
        assert len(data["recent"]) == 1


class TestModelEfficiency:
    def test_only_final_rollups(self) -> None:
        db = InmemoryDatabaseAdapter()
        _seed_model_efficiency(db)
        result = HANDLER.query("model-efficiency", {}, db)
        data = result["data"]
        assert "summary" in data
        assert len(data["summary"]) > 0
        for entry in data["summary"]:
            assert entry["rollup_status"] == "final"


class TestDlq:
    def test_returns_messages_and_breakdown(self) -> None:
        db = InmemoryDatabaseAdapter()
        _seed_dlq(db)
        result = HANDLER.query("dlq", {}, db)
        data = result["data"]
        assert "messages" in data
        assert "error_breakdown" in data
        assert "total" in data
        assert data["total"] == 1


class TestEvalResults:
    def test_returns_latest_report(self) -> None:
        db = InmemoryDatabaseAdapter()
        _seed_eval_results(db)
        result = HANDLER.query("eval-results", {}, db)
        data = result["data"]
        assert "reports" in data
        assert len(data["reports"]) == 1
        assert data["reports"][0]["suite_id"] == "core-v1"


class TestIntentBreakdown:
    def test_returns_grouped_counts(self) -> None:
        db = InmemoryDatabaseAdapter()
        _seed_intent_breakdown(db)
        result = HANDLER.query("intent-breakdown", {}, db)
        breakdown = result["data"]["breakdown"]
        assert isinstance(breakdown, list)
        assert len(breakdown) > 0
        types = {b["intent_type"] for b in breakdown}
        assert "debug" in types
        assert "refactor" in types


class TestHandleProtocol:
    """Test the RuntimeLocal handle() shim."""

    def test_handle_delegates_to_query(self) -> None:
        db = InmemoryDatabaseAdapter()
        _seed_staleness(db)
        result = HANDLER.handle(
            {
                "_db": db,
                "shape": "staleness",
                "params": {},
            }
        )
        assert isinstance(result, dict)
        assert result["shape"] == "staleness"


class TestContractIntegrity:
    def test_contract_declares_all_shapes_in_golden_path(self) -> None:
        contract_path = "src/omnimarket/nodes/node_projection_query/contract.yaml"
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        golden_path = contract.get("golden_path", [])
        golden_shapes = {step.get("shape") for step in golden_path if "shape" in step}
        assert set(SHAPES).issubset(golden_shapes)

    def test_contract_declares_command_topic(self) -> None:
        contract_path = "src/omnimarket/nodes/node_projection_query/contract.yaml"
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        topics = contract["event_bus"]["subscribe_topics"]
        assert "onex.cmd.omnimarket.projection-query-requested.v1" in topics
