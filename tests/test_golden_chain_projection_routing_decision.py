"""Golden chain tests for node_projection_routing_decision.

Closes the golden "registration" chain:
  onex.evt.omniclaude.routing-decision.v1 -> agent_routing_decisions

Includes the row-delta proof pattern (before=0, publish, after=1) required by
OMN-13122.
"""

from __future__ import annotations

import yaml

from omnimarket.nodes.node_projection_routing_decision.handlers.handler_projection_routing_decision import (
    HandlerProjectionRoutingDecision,
    ModelRoutingDecisionEvent,
)
from omnimarket.nodes.node_projection_routing_decision.handlers.handler_routing_decision import (
    KNOWN_PROJECTION_TABLES,
    RoutingDecisionProjectionRunner,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionRoutingDecision()

# A realistic routing-decision.v1 payload as emitted by omniclaude's
# HandlerRoutingEmitter (ModelRoutingDecision-shaped).
_ROUTING_PAYLOAD: dict[str, object] = {
    "id": "11111111-1111-1111-1111-111111111111",
    "correlation_id": "22222222-2222-2222-2222-222222222222",
    "claude_session_id": "sess-abc",
    "selected_agent": "agent-coder",
    "confidence_score": 0.9123,
    "created_at": "2026-06-13T10:00:00Z",
    "routing_reason": "policy:default",
    "request_type": "code-review",
    "alternatives": ["agent-reviewer", "agent-planner"],
    "domain": "engineering",
}


class TestRoutingDecisionProjection:
    def test_project_single_event(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelRoutingDecisionEvent.model_validate(_ROUTING_PAYLOAD)
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query("agent_routing_decisions")
        assert len(rows) == 1
        assert rows[0]["correlation_id"] == "22222222-2222-2222-2222-222222222222"
        assert rows[0]["selected_agent"] == "agent-coder"

    def test_row_delta_before_zero_after_one(self) -> None:
        """OMN-13122 row-delta proof: before=0, publish routing-decision, after=1."""
        db = InmemoryDatabaseAdapter()
        before = db.query("agent_routing_decisions")
        assert len(before) == 0

        event = ModelRoutingDecisionEvent.model_validate(_ROUTING_PAYLOAD)
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1

        after = db.query("agent_routing_decisions")
        assert len(after) == 1
        assert len(after) - len(before) == 1

    def test_dedup_by_id(self) -> None:
        """Append-only: same id (ON CONFLICT DO NOTHING) -> no second row."""
        db = InmemoryDatabaseAdapter()
        event = ModelRoutingDecisionEvent.model_validate(_ROUTING_PAYLOAD)
        first = HANDLER.project(event, db)
        second = HANDLER.project(event, db)
        assert first.rows_upserted == 1
        assert second.rows_upserted == 0
        rows = db.query("agent_routing_decisions")
        assert len(rows) == 1

    def test_missing_id_generates_uuid(self) -> None:
        """Sparse emitters without id still land a row with a generated key."""
        db = InmemoryDatabaseAdapter()
        payload = {k: v for k, v in _ROUTING_PAYLOAD.items() if k != "id"}
        event = ModelRoutingDecisionEvent.model_validate(payload)
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query("agent_routing_decisions")
        assert len(rows) == 1
        assert rows[0]["id"]

    def test_missing_created_at_uses_default(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload = {k: v for k, v in _ROUTING_PAYLOAD.items() if k != "created_at"}
        event = ModelRoutingDecisionEvent.model_validate(payload)
        HANDLER.project(event, db)
        rows = db.query("agent_routing_decisions")
        assert rows[0]["created_at"] is not None

    def test_project_batch(self) -> None:
        db = InmemoryDatabaseAdapter()
        events = [
            ModelRoutingDecisionEvent(
                id=f"id-{i:03d}",
                correlation_id=f"corr-{i:03d}",
                selected_agent=f"agent-{i}",
            )
            for i in range(4)
        ]
        result = HANDLER.project_batch(events, db)
        assert result.rows_upserted == 4
        assert len(db.query("agent_routing_decisions")) == 4

    def test_extra_fields_ignored(self) -> None:
        """Event model with extra='ignore' accepts unknown fields."""
        event = ModelRoutingDecisionEvent.model_validate(
            {**_ROUTING_PAYLOAD, "unknown_field": "ignored"}
        )
        assert event.selected_agent == "agent-coder"

    def test_event_bus_wiring(self) -> None:
        contract_path = (
            "src/omnimarket/nodes/node_projection_routing_decision/contract.yaml"
        )
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert (
            contract["handler"]["module"]
            == "omnimarket.nodes.node_projection_routing_decision."
            "handlers.handler_projection_routing_decision"
        )
        assert contract["handler"]["class"] == "HandlerProjectionRoutingDecision"
        topics = contract["event_bus"]["subscribe_topics"]
        assert "onex.evt.omniclaude.routing-decision.v1" in topics
        assert len(contract["event_bus"]["publish_topics"]) >= 1

    def test_db_io_targets_agent_routing_decisions(self) -> None:
        contract_path = (
            "src/omnimarket/nodes/node_projection_routing_decision/contract.yaml"
        )
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        tables = contract["db_io"]["db_tables"]
        names = {t["name"] for t in tables}
        assert "agent_routing_decisions" in names
        by_role = {t["role"]: t["name"] for t in tables}
        assert by_role["routing_decisions"] == "agent_routing_decisions"


class TestRoutingDecisionProjectionRunner:
    def test_runner_resolves_routing_table_role(self) -> None:
        runner = RoutingDecisionProjectionRunner()
        assert runner._table_routing == "agent_routing_decisions"

    def test_runner_subscribes_routing_decision_topic(self) -> None:
        runner = RoutingDecisionProjectionRunner()
        assert "onex.evt.omniclaude.routing-decision.v1" in runner.subscribe_topics

    def test_agent_routing_decisions_is_known_projection_table(self) -> None:
        assert "agent_routing_decisions" in KNOWN_PROJECTION_TABLES


class TestRoutingDecisionOwnership:
    def test_metadata_declares_infra_as_ddl_owner(self) -> None:
        """agent_routing_decisions DDL is owned by omnibase_infra (migration 021).

        This node is a non-owner writer and must not add a duplicate CREATE TABLE.
        """
        metadata_path = (
            "src/omnimarket/nodes/node_projection_routing_decision/metadata.yaml"
        )
        with open(metadata_path) as f:
            metadata = yaml.safe_load(f)
        ownership = metadata["ownership"]["agent_routing_decisions"]
        assert ownership["ddl_owner"] == "omnibase_infra"
        assert (
            ownership["duplicate_migration_policy"] == "forbid_cross_repo_create_table"
        )
        assert "omnimarket" in ownership["non_owner_repos"]
