# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Swarm dispatch chain on an in-memory bus: one command to one terminal event.

``test_swarm_schema_alignment`` proves the orchestrator's COMMAND payloads
validate against each downstream request model. This module proves the other
direction as well: each downstream node's real RESULT, published as the event
payload, is parsed by the orchestrator, and the run reaches exactly one
terminal event. Every hop is the node's real handler, wired to the orchestrator
through an in-memory bus that routes each command topic to its handler and each
result back to ``route_event`` on the matching event topic. Only the endpoint
HTTP probe and the delegation call (inference) are faked; no network is used.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, cast

import pytest
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_swarm_aggregator_compute.handlers.handler_swarm_aggregator import (
    HandlerSwarmAggregator,
)
from omnimarket.nodes.node_swarm_aggregator_compute.models.model_swarm_aggregate_request import (
    ModelSwarmAggregateRequest,
)
from omnimarket.nodes.node_swarm_decomposer_compute.handlers.handler_swarm_decomposer import (
    HandlerSwarmDecomposer,
)
from omnimarket.nodes.node_swarm_decomposer_compute.models.model_swarm_decompose_request import (
    ModelSwarmDecomposeRequest,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.handlers.handler_swarm_dispatch import (
    HandlerSwarmDispatchOrchestrator,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_swarm_dispatch_request import (
    ModelSwarmDispatchRequest,
)
from omnimarket.nodes.node_swarm_endpoint_health_effect.handlers.handler_swarm_endpoint_health import (
    HandlerSwarmEndpointHealth,
)
from omnimarket.nodes.node_swarm_endpoint_health_effect.models.model_swarm_health_check_request import (
    ModelSwarmHealthCheckRequest,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.handlers.handler_swarm_fanout import (
    HandlerSwarmFanout,
    resolve_completion_topics,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_fanout_request import (
    ModelSwarmFanoutRequest,
)
from omnimarket.nodes.node_swarm_registry_compute.handlers.handler_swarm_registry import (
    HandlerSwarmRegistry,
)
from omnimarket.nodes.node_swarm_registry_compute.models.model_swarm_endpoint_selection_request import (
    ModelSwarmEndpointSelectionRequest,
)

_NODES = Path(__file__).parent.parent / "src" / "omnimarket" / "nodes"


def _contract(node: str) -> Path:
    return _NODES / node / "contract.yaml"


def _cmd_topic(node: str) -> str:
    return contract_subscribe_topics(_contract(node))[0]


def _evt_topic(node: str) -> str:
    return contract_publish_topics(_contract(node))[0]


_ORCHESTRATOR = "node_swarm_dispatch_orchestrator"
_TOPIC_DISPATCH = _cmd_topic(_ORCHESTRATOR)
_TOPIC_HEALTH_CMD = _cmd_topic("node_swarm_endpoint_health_effect")
_TOPIC_DECOMPOSE_CMD = _cmd_topic("node_swarm_decomposer_compute")
_TOPIC_SELECT_CMD = _cmd_topic("node_swarm_registry_compute")
_TOPIC_FANOUT_CMD = _cmd_topic("node_swarm_fanout_orchestrator")
_TOPIC_AGGREGATE_CMD = _cmd_topic("node_swarm_aggregator_compute")
_TOPIC_COMPLETED, _TOPIC_FAILED = (
    t
    for t in contract_publish_topics(_contract(_ORCHESTRATOR))
    if "swarm-dispatch-" in t
)

_RESULT_TOPICS: dict[str, str] = {
    _TOPIC_HEALTH_CMD: _evt_topic("node_swarm_endpoint_health_effect"),
    _TOPIC_DECOMPOSE_CMD: _evt_topic("node_swarm_decomposer_compute"),
    _TOPIC_SELECT_CMD: _evt_topic("node_swarm_registry_compute"),
    _TOPIC_FANOUT_CMD: _evt_topic("node_swarm_fanout_orchestrator"),
    _TOPIC_AGGREGATE_CMD: _evt_topic("node_swarm_aggregator_compute"),
}

_ANSWER = "ok"
_RUN_ID = "run-chain-001"
_CORR_ID = "corr-chain-001"

# Documentation-range addresses (RFC 5737): nothing is ever contacted.
_REGISTRY_YAML = """\
registry_schema_version: "1.0.0"
endpoints:
  - id: "ep-a"
    base_url: "http://192.0.2.10:8000/v1"
    model_id: "model-a"
    endpoint_ref: "LLM_EP_A_URL"
    provider: "vllm"
    capabilities: [general]
    health_check_path: "/health"
  - id: "ep-b"
    base_url: "http://192.0.2.11:8000/v1"
    model_id: "model-b"
    endpoint_ref: "LLM_EP_B_URL"
    provider: "vllm"
    capabilities: [general]
    health_check_path: "/health"
"""

_HttpGet = Callable[[str, float], Coroutine[Any, Any, tuple[int, bytes]]]


def _http_all(health_status: int) -> _HttpGet:
    """Fake probe: ``/health`` answers *health_status*; ``/v1/models`` lists both models."""

    async def _get(url: str, timeout: float) -> tuple[int, bytes]:
        if url.endswith("/v1/models"):
            body = {"data": [{"id": "model-a"}, {"id": "model-b"}]}
            return 200, json.dumps(body).encode()
        return health_status, b""

    return _get


def _http_by_host(statuses: dict[str, int]) -> _HttpGet:
    async def _get(url: str, timeout: float) -> tuple[int, bytes]:
        if url.endswith("/v1/models"):
            body = {"data": [{"id": "model-a"}, {"id": "model-b"}]}
            return 200, json.dumps(body).encode()
        for host, status in statuses.items():
            if host in url:
                return status, b""
        return 500, b""

    return _get


class _MemoryBus:
    """Records every publish as ``(topic, payload)``; payloads unwrapped from the envelope."""

    def __init__(self) -> None:
        self.queue: list[tuple[str, dict[str, Any]]] = []
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        envelope = json.loads(value)
        item = (topic, envelope["payload"])
        self.queue.append(item)
        self.published.append(item)

    def topics(self) -> list[str]:
        return [t for t, _ in self.published]

    def payloads(self, topic: str) -> list[dict[str, Any]]:
        return [p for t, p in self.published if t == topic]


class _FakeDelegation:
    """Stands in for the delegation call effect: one completion per delegation-execute."""

    def __init__(self) -> None:
        self.commands: list[dict[str, object]] = []
        self._completed_topic = next(
            t for t in resolve_completion_topics() if "delegation-call-completed" in t
        )

    def publish(self, topic: str, payload: dict[str, object]) -> None:
        self.commands.append(payload)

    def poll(
        self, topics: list[str], run_id: str, timeout_seconds: float
    ) -> list[dict[str, object]]:
        return [
            {
                "correlation_id": cmd["correlation_id"],
                "request_id": cmd["request_id"],
                "success": True,
                "content": _ANSWER,
                "model_id": cmd["model_id"],
                "latency_ms": 12,
                "_topic": self._completed_topic,
            }
            for cmd in self.commands
        ]


async def _run_chain(
    tmp_path: Path,
    http_get: _HttpGet,
    endpoint_ids: tuple[str, ...] = ("ep-a", "ep-b"),
) -> tuple[_MemoryBus, _FakeDelegation, HandlerSwarmDispatchOrchestrator]:
    registry = tmp_path / "endpoint_registry.yaml"
    registry.write_text(_REGISTRY_YAML)

    bus = _MemoryBus()
    delegation = _FakeDelegation()
    orchestrator = HandlerSwarmDispatchOrchestrator(
        event_bus=cast(ProtocolEventBusPublisher, bus)
    )
    health = HandlerSwarmEndpointHealth(http_get_fn=http_get, registry_path=registry)
    decomposer = HandlerSwarmDecomposer()
    selector = HandlerSwarmRegistry(registry_path=registry)
    fanout = HandlerSwarmFanout(
        queue_publisher=delegation,
        queue_subscriber=delegation,
        registry_path=registry,
    )
    aggregator = HandlerSwarmAggregator()

    await orchestrator.handle_async(
        ModelSwarmDispatchRequest(
            task="reply with the single word ok",
            endpoint_ids=endpoint_ids,
            max_subtasks=2,
            run_id=_RUN_ID,
            correlation_id=_CORR_ID,
        )
    )

    while bus.queue:
        topic, payload = bus.queue.pop(0)
        result: Any
        if topic == _TOPIC_HEALTH_CMD:
            result = await health.handle(ModelSwarmHealthCheckRequest(**payload))
        elif topic == _TOPIC_DECOMPOSE_CMD:
            result = decomposer.handle(ModelSwarmDecomposeRequest(**payload))
        elif topic == _TOPIC_SELECT_CMD:
            result = selector.handle(ModelSwarmEndpointSelectionRequest(**payload))
        elif topic == _TOPIC_FANOUT_CMD:
            result = fanout.handle(ModelSwarmFanoutRequest(**payload))
        elif topic == _TOPIC_AGGREGATE_CMD:
            result = aggregator.handle(ModelSwarmAggregateRequest(**payload))
        else:
            continue  # terminal events are only recorded
        await orchestrator.route_event(
            _RESULT_TOPICS[topic], result.model_dump(mode="json")
        )
    return bus, delegation, orchestrator


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_healthy_endpoints_reach_exactly_one_completed_event(
    tmp_path: Path,
) -> None:
    bus, delegation, _ = await _run_chain(tmp_path, _http_all(200))

    assert len(bus.payloads(_TOPIC_COMPLETED)) == 1
    assert bus.payloads(_TOPIC_FAILED) == []
    completed = bus.payloads(_TOPIC_COMPLETED)[0]
    assert completed["run_id"] == _RUN_ID
    assert completed["status"] == "succeeded"
    # the one subtask went out as a delegation command and its answer came back
    assert len(delegation.commands) == 1
    assert completed["subtask_count"] == 1
    assert completed["succeeded_count"] == 1
    assert _ANSWER in completed["aggregated_output"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_endpoints_unhealthy_yields_one_failed_event_and_no_fanout(
    tmp_path: Path,
) -> None:
    bus, delegation, _ = await _run_chain(tmp_path, _http_all(503))

    assert len(bus.payloads(_TOPIC_FAILED)) == 1
    assert bus.payloads(_TOPIC_FAILED)[0]["run_id"] == _RUN_ID
    assert bus.payloads(_TOPIC_COMPLETED) == []
    assert bus.payloads(_TOPIC_FANOUT_CMD) == []
    assert delegation.commands == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_endpoint_ids_missing_from_registry_yield_one_failed_event(
    tmp_path: Path,
) -> None:
    """No probed endpoint at all is the same outcome as none healthy."""
    bus, delegation, _ = await _run_chain(
        tmp_path, _http_all(200), endpoint_ids=("not-in-registry",)
    )

    assert len(bus.payloads(_TOPIC_FAILED)) == 1
    assert bus.payloads(_TOPIC_COMPLETED) == []
    assert bus.payloads(_TOPIC_FANOUT_CMD) == []
    assert delegation.commands == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_healthy_one_auth_failed_endpoint_still_completes(
    tmp_path: Path,
) -> None:
    """An auth-failed sibling must not make the selection hop refuse the request."""
    bus, delegation, _ = await _run_chain(
        tmp_path, _http_by_host({"192.0.2.10": 200, "192.0.2.11": 401})
    )

    assert len(bus.payloads(_TOPIC_COMPLETED)) == 1
    assert bus.payloads(_TOPIC_FAILED) == []
    assert [c["model_id"] for c in delegation.commands] == ["model-a"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_event_for_a_finished_run_publishes_nothing_more(
    tmp_path: Path,
) -> None:
    """A redelivered event after the terminal event must not add a second one."""
    bus, _, orchestrator = await _run_chain(tmp_path, _http_all(503))
    assert len(bus.payloads(_TOPIC_FAILED)) == 1
    before = len(bus.published)

    await orchestrator.route_event(
        _RESULT_TOPICS[_TOPIC_HEALTH_CMD],
        {"run_id": _RUN_ID, "endpoint_health": {}},
    )

    assert len(bus.published) == before


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_phase_event_fails_the_run_instead_of_raising(
    tmp_path: Path,
) -> None:
    """A phase result the orchestrator cannot parse ends the run with a failed event."""
    bus = _MemoryBus()
    orchestrator = HandlerSwarmDispatchOrchestrator(
        event_bus=cast(ProtocolEventBusPublisher, bus)
    )
    await orchestrator.handle_async(
        ModelSwarmDispatchRequest(
            task="reply with the single word ok",
            endpoint_ids=("ep-a",),
            run_id="run-chain-malformed",
            correlation_id=_CORR_ID,
        )
    )
    health_event = {
        "run_id": "run-chain-malformed",
        "endpoint_health": {"ep-a": {"endpoint_status": "reachable"}},
    }
    await orchestrator.route_event(_RESULT_TOPICS[_TOPIC_HEALTH_CMD], health_event)

    state = await orchestrator.route_event(
        _RESULT_TOPICS[_TOPIC_DECOMPOSE_CMD],
        {"run_id": "run-chain-malformed", "decomposition": {"subtasks": [{}]}},
    )

    assert state.fsm_state.value == "failed"
    assert len(bus.payloads(_TOPIC_FAILED)) == 1
    assert bus.payloads(_TOPIC_SELECT_CMD) == []
