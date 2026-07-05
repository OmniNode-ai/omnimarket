"""In-node golden-chain coverage for node_projection_routing_decision.

Co-located with the node (under src/) so the dependency-health sweep
(`--repo-roots src/`) recognizes test coverage for the contract-referenced
handlers handler_projection_routing_decision and handler_routing_decision.

Exercises the routing-decision registration chain (OMN-13122):
onex.evt.omniclaude.routing-decision.v1 -> agent_routing_decisions. The broader
assertion matrix lives in tests/test_golden_chain_projection_routing_decision.py;
this module keeps a self-contained row-delta proof plus the runner-wiring check
next to the node so both handlers are visibly covered.
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_routing_decision.handlers.handler_projection_routing_decision import (
    HandlerProjectionRoutingDecision,
    ModelRoutingDecisionEvent,
)
from omnimarket.nodes.node_projection_routing_decision.handlers.handler_routing_decision import (
    KNOWN_PROJECTION_TABLES,
    RoutingDecisionProjectionRunner,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_TABLE = "agent_routing_decisions"
_DECISION_ID = "11111111-1111-1111-1111-111111111111"
_CORRELATION_ID = "22222222-2222-2222-2222-222222222222"

# A realistic routing-decision.v1 payload as emitted by omniclaude's
# HandlerRoutingEmitter (ModelRoutingDecision-shaped).
_ROUTING_PAYLOAD: dict[str, object] = {
    "id": _DECISION_ID,
    "correlation_id": _CORRELATION_ID,
    "claude_session_id": "sess-abc",
    "selected_agent": "agent-coder",
    "confidence_score": 0.9123,
    "created_at": "2026-06-13T10:00:00Z",
    "routing_reason": "policy:default",
    "request_type": "code-review",
    "alternatives": ["agent-reviewer", "agent-planner"],
    "domain": "engineering",
}


def test_row_delta_before_zero_after_one() -> None:
    """OMN-13122 row-delta proof: before=0, project one terminal, after=1 row.

    Covers the sync projection handler HandlerProjectionRoutingDecision.
    """
    handler = HandlerProjectionRoutingDecision()
    db = InmemoryDatabaseAdapter()
    before = db.query(_TABLE)
    assert len(before) == 0

    result = handler.project(
        ModelRoutingDecisionEvent.model_validate(_ROUTING_PAYLOAD), db
    )

    assert result.rows_upserted == 1
    after = db.query(_TABLE)
    assert len(after) == 1
    assert len(after) - len(before) == 1
    assert after[0]["id"] == _DECISION_ID
    assert after[0]["correlation_id"] == _CORRELATION_ID
    assert after[0]["selected_agent"] == "agent-coder"


def test_append_only_dedup_by_id() -> None:
    """Append-only: same id (ON CONFLICT (id) DO NOTHING) -> no second row."""
    handler = HandlerProjectionRoutingDecision()
    db = InmemoryDatabaseAdapter()
    event = ModelRoutingDecisionEvent.model_validate(_ROUTING_PAYLOAD)
    first = handler.project(event, db)
    second = handler.project(event, db)
    assert first.rows_upserted == 1
    assert second.rows_upserted == 0
    assert len(db.query(_TABLE)) == 1


def test_runner_resolves_routing_table_and_topic() -> None:
    """Covers the async consume-leg runner RoutingDecisionProjectionRunner.

    Constructing the runner validates its contract-declared table role against
    KNOWN_PROJECTION_TABLES (raises otherwise), so a successful construction is
    proof the routing_decisions role resolved. Topic wiring is asserted via the
    public subscribe_topics surface.
    """
    assert _TABLE in KNOWN_PROJECTION_TABLES
    runner = RoutingDecisionProjectionRunner()
    assert "onex.evt.omniclaude.routing-decision.v1" in runner.subscribe_topics
    assert runner.topics == runner.subscribe_topics
