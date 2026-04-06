"""Golden chain tests for node_projection_delegation."""

from __future__ import annotations

import yaml

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
    ModelTaskDelegatedEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionDelegation()


class TestDelegationProjection:
    def test_project_single_event(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-001",
            task_type="code-review",
            delegated_to="agent-alpha",
            delegated_by="team-lead",
            quality_gate_passed=True,
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query("delegation_events")
        assert len(rows) == 1
        assert rows[0]["task_type"] == "code-review"
        assert rows[0]["quality_gate_passed"] is True

    def test_dedup_by_correlation_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelTaskDelegatedEvent(
                correlation_id="corr-001",
                task_type="refactor",
                delegated_to="agent-a",
            ),
            db,
        )
        HANDLER.project(
            ModelTaskDelegatedEvent(
                correlation_id="corr-001",
                task_type="test-generation",
                delegated_to="agent-b",
            ),
            db,
        )
        rows = db.query("delegation_events")
        assert len(rows) == 1
        # Second write wins (UPSERT)
        assert rows[0]["task_type"] == "test-generation"

    def test_project_batch(self) -> None:
        db = InmemoryDatabaseAdapter()
        events = [
            ModelTaskDelegatedEvent(
                correlation_id=f"corr-{i:03d}",
                task_type="code-review",
                delegated_to=f"agent-{i}",
            )
            for i in range(3)
        ]
        result = HANDLER.project_batch(events, db)
        assert result.rows_upserted == 3

    def test_shadow_delegation(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-shadow",
            task_type="code-review",
            delegated_to="shadow-agent",
            is_shadow=True,
        )
        HANDLER.project(event, db)
        rows = db.query("delegation_events", {"is_shadow": True})
        assert len(rows) == 1

    def test_event_bus_wiring(self) -> None:
        contract_path = "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert (
            "onex.evt.omniclaude.task-delegated.v1"
            in contract["event_bus"]["subscribe_topics"]
        )
