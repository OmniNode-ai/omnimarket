"""Golden chain tests for node_nightly_loop_controller.

Proves: config -> handler -> decisions -> iterations -> projection -> API
Uses InmemoryDatabaseAdapter, zero infra required.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import yaml

from omnimarket.nodes.node_nightly_loop_controller.handlers.handler_nightly_loop_controller import (
    HandlerNightlyLoopController,
)
from omnimarket.nodes.node_nightly_loop_controller.handlers.handler_projection_nightly_loop import (
    HandlerProjectionNightlyLoop,
    ModelNightlyLoopDecisionEvent,
    ModelNightlyLoopIterationEvent,
)
from omnimarket.nodes.node_nightly_loop_controller.models.model_nightly_loop import (
    DecisionOutcome,
    GapStatus,
    ModelDelegationRoute,
    ModelNightlyLoopConfig,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT_PATH = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_nightly_loop_controller"
    / "contract.yaml"
)

HANDLER = HandlerNightlyLoopController()
PROJECTION = HandlerProjectionNightlyLoop()


class TestNightlyLoopController:
    """Tests for the core handler: config -> decisions -> iterations."""

    def test_handle_returns_metadata(self) -> None:
        result = HANDLER.handle()
        assert result["status"] == "ok"
        assert result["handler"] == "HandlerNightlyLoopController"
        assert "nightly_loop_decisions" in result["tables"]
        assert "nightly_loop_iterations" in result["tables"]

    def test_single_iteration_with_priorities(self) -> None:
        db = InmemoryDatabaseAdapter()
        config = ModelNightlyLoopConfig(
            priorities=("golden-chain-coverage", "tech-debt"),
            max_iterations_per_run=1,
        )
        result = HANDLER.run(config=config, db=db)
        assert result.iterations_completed == 1
        assert result.iterations_failed == 0
        assert result.total_decisions >= 2
        assert len(result.decisions) >= 2

        # Decisions persisted to DB
        rows = db.query("nightly_loop_decisions")
        assert len(rows) >= 2

        # Iteration persisted to DB
        iters = db.query("nightly_loop_iterations")
        assert len(iters) == 1

    def test_routing_table_used(self) -> None:
        db = InmemoryDatabaseAdapter()
        route = ModelDelegationRoute(
            task_type="golden-chain",
            model_endpoint="http://192.168.86.201:8001",
            model_id="Qwen/Qwen3-14B-AWQ",
            cost_per_call_usd=0.001,
            is_frontier=False,
        )
        config = ModelNightlyLoopConfig(
            priorities=("golden-chain-coverage",),
            routing_table=(route,),
            max_iterations_per_run=1,
        )
        result = HANDLER.run(config=config, db=db)
        assert result.total_decisions >= 1

        # Check decision used the routed model
        dispatched = [
            d
            for d in result.decisions
            if d.action == "dispatch-ticket" and d.outcome == DecisionOutcome.success
        ]
        assert len(dispatched) >= 1
        assert dispatched[0].model_used == "Qwen/Qwen3-14B-AWQ"
        assert dispatched[0].cost_usd == 0.001

    def test_gap_tracking(self) -> None:
        db = InmemoryDatabaseAdapter()
        config = ModelNightlyLoopConfig(
            priorities=(),
            active_gaps=("GAP-001", "GAP-002"),
            max_iterations_per_run=1,
        )
        result = HANDLER.run(config=config, db=db)
        assert result.total_gaps_checked == 2
        assert result.gap_status["GAP-001"] == GapStatus.in_progress
        assert result.gap_status["GAP-002"] == GapStatus.in_progress

    def test_cost_ceiling_stops_iterations(self) -> None:
        db = InmemoryDatabaseAdapter()
        route = ModelDelegationRoute(
            task_type="expensive",
            model_endpoint="https://api.openai.com/v1",
            model_id="gpt-4o",
            cost_per_call_usd=3.0,
            is_frontier=True,
        )
        config = ModelNightlyLoopConfig(
            priorities=("expensive-task",),
            routing_table=(route,),
            max_iterations_per_run=5,
            max_cost_usd_per_run=5.0,
        )
        result = HANDLER.run(config=config, db=db)
        # Should stop after 2 iterations (3.0 + 3.0 = 6.0 > 5.0 ceiling)
        assert result.iterations_completed <= 2
        assert result.total_cost_usd <= 6.0

    def test_dry_run_skips_persistence(self) -> None:
        db = InmemoryDatabaseAdapter()
        config = ModelNightlyLoopConfig(
            priorities=("test-task",),
            max_iterations_per_run=1,
        )
        result = HANDLER.run(config=config, db=db, dry_run=True)
        assert result.iterations_completed == 1

        # Dry run should NOT write to DB
        decisions = db.query("nightly_loop_decisions")
        iterations = db.query("nightly_loop_iterations")
        assert len(decisions) == 0
        assert len(iterations) == 0

        # But should still track decisions in result
        assert result.total_decisions >= 1
        assert all(
            d.outcome == DecisionOutcome.skipped
            for d in result.decisions
            if d.action == "dispatch-ticket"
        )

    def test_multiple_iterations(self) -> None:
        db = InmemoryDatabaseAdapter()
        config = ModelNightlyLoopConfig(
            priorities=("task-a", "task-b"),
            max_iterations_per_run=3,
        )
        result = HANDLER.run(config=config, db=db)
        assert result.iterations_completed == 3
        assert len(result.iterations) == 3

        iters = db.query("nightly_loop_iterations")
        assert len(iters) == 3

    def test_correlation_id_propagated(self) -> None:
        db = InmemoryDatabaseAdapter()
        corr_id = uuid4()
        config = ModelNightlyLoopConfig(
            priorities=("task-a",),
            max_iterations_per_run=1,
        )
        result = HANDLER.run(config=config, db=db, correlation_id=corr_id)
        assert result.correlation_id == corr_id
        assert all(d.correlation_id == corr_id for d in result.decisions)
        assert all(it.correlation_id == corr_id for it in result.iterations)


class TestProjectionNightlyLoop:
    """Tests for the projection handler: events -> DB tables."""

    def test_project_decision(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelNightlyLoopDecisionEvent(
            decision_id="dec-001",
            iteration_id="iter-001",
            correlation_id="corr-001",
            action="dispatch-ticket",
            target="OMN-1234",
            outcome="success",
            model_used="Qwen/Qwen3-14B-AWQ",
            cost_usd=0.001,
        )
        result = PROJECTION.project_decision(event, db)
        assert result.rows_upserted == 1
        rows = db.query("nightly_loop_decisions")
        assert len(rows) == 1
        assert rows[0]["action"] == "dispatch-ticket"
        assert rows[0]["target"] == "OMN-1234"

    def test_project_iteration(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelNightlyLoopIterationEvent(
            iteration_id="iter-001",
            correlation_id="corr-001",
            iteration_number=1,
            gaps_checked=3,
            gaps_closed=1,
            decisions_made=5,
            tickets_dispatched=2,
            total_cost_usd=0.003,
        )
        result = PROJECTION.project_iteration(event, db)
        assert result.rows_upserted == 1
        rows = db.query("nightly_loop_iterations")
        assert len(rows) == 1
        assert rows[0]["gaps_checked"] == 3
        assert rows[0]["tickets_dispatched"] == 2

    def test_decision_dedup(self) -> None:
        db = InmemoryDatabaseAdapter()
        event1 = ModelNightlyLoopDecisionEvent(
            decision_id="dec-001",
            iteration_id="iter-001",
            correlation_id="corr-001",
            action="dispatch-ticket",
            target="OMN-1234",
            outcome="success",
        )
        event2 = ModelNightlyLoopDecisionEvent(
            decision_id="dec-001",
            iteration_id="iter-001",
            correlation_id="corr-001",
            action="dispatch-ticket",
            target="OMN-1234",
            outcome="failure",
        )
        PROJECTION.project_decision(event1, db)
        PROJECTION.project_decision(event2, db)
        rows = db.query("nightly_loop_decisions")
        assert len(rows) == 1
        # Second write wins (UPSERT)
        assert rows[0]["outcome"] == "failure"

    def test_projection_handler_metadata(self) -> None:
        result = PROJECTION.handle()
        assert result["status"] == "ok"
        assert result["mode"] == "projection"


class TestContractWiring:
    """Tests for contract.yaml correctness."""

    def test_contract_topics(self) -> None:
        with open(_CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)

        assert (
            "onex.cmd.omnimarket.nightly-loop-start.v1"
            in contract["event_bus"]["subscribe_topics"]
        )
        assert (
            "onex.evt.omnimarket.nightly-loop-decision.v1"
            in contract["event_bus"]["publish_topics"]
        )
        assert (
            "onex.evt.omnimarket.nightly-loop-iteration-completed.v1"
            in contract["event_bus"]["publish_topics"]
        )
        assert (
            "onex.evt.omnimarket.nightly-loop-completed.v1"
            in contract["event_bus"]["publish_topics"]
        )

    def test_contract_handler_reference(self) -> None:
        with open(_CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)

        handlers = contract["handler_routing"]["handlers"]
        assert any(
            h.get("handler", {}).get("name") == "HandlerNightlyLoopController"
            for h in handlers
        )

    def test_contract_env_deps(self) -> None:
        with open(_CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)

        assert "OMNIBASE_INFRA_DB_URL" in contract["env_deps"]
        assert "KAFKA_BOOTSTRAP_SERVERS" in contract["env_deps"]
