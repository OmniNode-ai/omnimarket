# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""routing-feedback-updated.v1 has a producer that publishes it and a consumer.

Before this wiring the feedback reducer's handler was non-canonical (a raw
``object`` entrypoint that returned a dict the runtime drops), so nothing was
ever published, and no node subscribed to the topic: the learn-from-outcomes
loop computed feedback and dropped it. These tests drive the REAL auto-wiring
dispatch callback for both ends and fail while either end is unwired.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _make_dispatch_callback,
    _node_kind_from_node_type,
)
from omnibase_infra.runtime.event_bus_subcontract_wiring import (
    load_published_events_map,
)

from omnimarket.events.topics import ROUTING_FEEDBACK_UPDATED_TOPIC_V1
from omnimarket.models.delegation.model_routing_feedback import (
    ModelRoutingFeedback,
    ModelRoutingFeedbackUpdatedEvent,
)
from omnimarket.nodes.node_delegation_routing_feedback_reducer.handlers.handler_delegation_routing_feedback import (
    HandlerDelegationRoutingFeedback,
)
from omnimarket.nodes.node_projection_routing_feedback.handlers import (
    HandlerProjectionRoutingFeedback,
)
from omnimarket.nodes.node_projection_routing_feedback.models import (
    ModelRoutingFeedbackProjectionResult,
)

pytestmark = pytest.mark.unit
_NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
_PRODUCER = "node_delegation_routing_feedback_reducer"
_CONSUMER = "node_projection_routing_feedback"


def _contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((_NODES / node / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _completed(*, success: bool = True, latency_ms: int = 200) -> dict[str, Any]:
    return {
        "correlation_id": "corr-1",
        "causation_id": "cause-1",
        "request_id": "req-1",
        "task_type": "codegen",
        "task_id": None,
        "selected_model": "qwen3-coder-30b",
        "model_id": "qwen3-coder-30b",
        "model_tier": "cheap_local",
        "provider": "bifrost",
        "endpoint_ref": "BIFROST_URL",
        "tokens_in": 100,
        "tokens_out": 50,
        "latency_ms": latency_ms,
        "success": success,
        "escalated_to": None,
    }


def _escalation() -> dict[str, Any]:
    return {
        "correlation_id": "corr-2",
        "causation_id": "cause-2",
        "request_id": "req-2",
        "task_type": "codegen",
        "task_id": None,
        "model_id": "qwen3-coder-30b",
        "attempt_number": 1,
        "escalation_reason": "timed out",
        "next_model_id": "claude",
    }


def _all_tiers_failed() -> dict[str, Any]:
    return {
        "correlation_id": "corr-3",
        "causation_id": "cause-3",
        "request_id": "req-3",
        "task_type": "codegen",
        "task_id": None,
        "attempted_models": ("fixture-model-a", "qwen3-coder-30b"),
    }


def _entry(node: str) -> tuple[Any, Any]:
    (contract,) = discover_contracts_from_paths(
        [_NODES / node / "contract.yaml"]
    ).contracts
    (entry,) = contract.handler_routing.handlers
    return contract, entry


def _callback(node: str, handler: Any) -> Any:
    contract, entry = _entry(node)
    return _make_dispatch_callback(
        handler,
        entry.event_model,
        handler_node_kind=_node_kind_from_node_type(contract.node_type),
        published_event_names=frozenset(
            load_published_events_map(contract.contract_path)
        ),
    )


def test_a_node_subscribes_to_routing_feedback_updated() -> None:
    """The topic is consumed inside the node graph, not parked as a sink."""
    subscribers = [
        node.name
        for node in _NODES.iterdir()
        if (node / "contract.yaml").is_file()
        and ROUTING_FEEDBACK_UPDATED_TOPIC_V1
        in ((_contract(node.name).get("event_bus") or {}).get("subscribe_topics") or [])
    ]
    assert subscribers == [_CONSUMER]


def test_producer_no_longer_declares_the_topic_an_external_sink() -> None:
    contract = _contract(_PRODUCER)
    assert ROUTING_FEEDBACK_UPDATED_TOPIC_V1 not in (
        contract.get("externally_consumed_topics") or []
    )
    assert ROUTING_FEEDBACK_UPDATED_TOPIC_V1 in contract["event_bus"]["publish_topics"]


def test_producer_maps_its_published_event_to_the_topic() -> None:
    """Without this map the runtime classifies a reducer's return as a projection."""
    published = _contract(_PRODUCER)["published_events"]
    assert {(e["event_type"], e["topic"]): None for e in published} == {
        ("RoutingFeedbackUpdatedEvent", ROUTING_FEEDBACK_UPDATED_TOPIC_V1): None
    }


@pytest.mark.parametrize(
    "payload",
    [_completed(), _completed(success=False), _escalation(), _all_tiers_failed()],
    ids=["completed", "completed-failed", "escalation", "all-tiers-failed"],
)
async def test_producer_dispatch_publishes_the_updated_event(
    payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real callback hands the raw terminal payload to a typed handler and
    the returned event reaches ``output_events`` (it was a dropped dict)."""
    monkeypatch.delenv("ONEX_CI_MODE", raising=False)
    callback = _callback(_PRODUCER, HandlerDelegationRoutingFeedback())
    result = await callback(ModelEventEnvelope[object](payload=payload))

    assert result is not None
    (event,) = result.output_events
    assert isinstance(event, ModelRoutingFeedbackUpdatedEvent)
    assert event.feedback.model_id in {"qwen3-coder-30b", "claude", "fixture-model-a"}
    assert event.feedback.task_type == "codegen"
    assert event.feedback.total_count == 1


async def test_producer_dispatch_accumulates_across_terminal_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_CI_MODE", raising=False)
    callback = _callback(_PRODUCER, HandlerDelegationRoutingFeedback())
    for payload in (_completed(), _completed(success=False), _escalation()):
        result = await callback(ModelEventEnvelope[object](payload=payload))
    assert result is not None
    (event,) = result.output_events
    assert (
        event.feedback.total_count,
        event.feedback.success_count,
        event.feedback.escalation_count,
    ) == (3, 1, 1)


async def test_empty_payload_is_a_noop_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_CI_MODE", raising=False)
    callback = _callback(_PRODUCER, HandlerDelegationRoutingFeedback())
    result = await callback(ModelEventEnvelope[object](payload={}))
    assert result is None or list(result.output_events) == []


async def test_published_event_flows_through_the_consumer_fold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Producer output, serialized as the bus would carry it, is consumable."""
    monkeypatch.delenv("ONEX_CI_MODE", raising=False)
    produced = await _callback(_PRODUCER, HandlerDelegationRoutingFeedback())(
        ModelEventEnvelope[object](payload=_completed())
    )
    assert produced is not None
    (event,) = produced.output_events
    wire = event.model_dump(mode="json")

    consumed = await _callback(_CONSUMER, HandlerProjectionRoutingFeedback())(
        ModelEventEnvelope[object](payload=wire)
    )
    assert consumed is not None
    # A projection result is captured as a projection intent, never re-published.
    assert list(consumed.output_events) == []
    result = HandlerProjectionRoutingFeedback().handle(
        ModelRoutingFeedbackUpdatedEvent.model_validate(wire)
    )
    assert isinstance(result, ModelRoutingFeedbackProjectionResult)
    (row,) = result.rows
    assert (row.model_id, row.task_type, row.total_count) == (
        "qwen3-coder-30b",
        "codegen",
        1,
    )


def test_fold_carries_the_cumulative_feedback_unchanged() -> None:
    feedback = ModelRoutingFeedback(
        model_id="m",
        task_type="t",
        success_count=2,
        failure_count=1,
        escalation_count=1,
        total_count=3,
        success_rate=2 / 3,
        escalation_rate=1 / 3,
        avg_latency_ms=120.0,
        window_start="2026-10-05T00:00:00+00:00",
        last_updated="2026-10-05T00:05:00+00:00",
    )
    event = ModelRoutingFeedbackUpdatedEvent(
        correlation_id="c", feedback=feedback, source_topic=""
    )
    (row,) = HandlerProjectionRoutingFeedback().handle(event).rows
    assert row.model_dump() == feedback.model_dump()
