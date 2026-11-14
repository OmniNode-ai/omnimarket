# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The chain harness: the gate's and the batching node's real contracts, the runtime's own dispatch, the in-memory bus."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.runtime.runtime_dispatch import RuntimeDispatch
from omnibase_core.runtime.transport.runtime_in_memory_broker import InMemoryBroker
from omnibase_core.runtime.transport.runtime_in_memory_transport import (
    InMemoryTransport,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.core_runtime.dlq_resolver import derive_canonical_dlq_topic
from omnibase_infra.runtime.core_runtime.routing_map_builder import build_routing_map

from omnimarket.events.runtime_deployment import ModelDeployPublishCommand
from omnimarket.models.dev_roll import ModelDevRollHoldRequest, ModelDevRollReleased
from omnimarket.nodes.node_dev_roll_gate_effect.handlers import (
    HandlerDevRollGateCompleted,
    HandlerDevRollGateDecided,
    HandlerDevRollGateHold,
    HandlerDevRollGateRejected,
    HandlerDevRollGateTick,
    InMemoryDevRollGateStore,
)
from omnimarket.nodes.node_dev_roll_gate_effect.models import ModelDevRollGateTick
from omnimarket.nodes.node_dev_roll_request_compute.handlers import (
    HandlerDevRollRequest,
)
from tests.unit.nodes.node_dev_roll_gate_effect.conftest import POLICY, Clock

ROOT = Path(__file__).resolve().parents[4]
GATE_CONTRACT = ROOT / "src/omnimarket/nodes/node_dev_roll_gate_effect/contract.yaml"
COMPUTE_CONTRACT = (
    ROOT / "src/omnimarket/nodes/node_dev_roll_request_compute/contract.yaml"
)
_GATE = yaml.safe_load(GATE_CONTRACT.read_text(encoding="utf-8"))
# The topics are read from the contract, the single place the node declares them.
_BY_OPERATION: dict[str, str] = {
    entry["operation"]: entry["topic"] for entry in _GATE["handler_routing"]["handlers"]
}
_BY_EVENT: dict[str, str] = {
    entry["event_type"]: entry["topic"] for entry in _GATE["published_events"]
}
HOLD_TOPIC: str = _BY_OPERATION["dev_roll_gate.hold"]
TICK_TOPIC: str = _BY_OPERATION["dev_roll_gate.tick"]
DECIDED_TOPIC: str = _BY_OPERATION["dev_roll_gate.decided"]
COMPLETED_TOPIC: str = _BY_OPERATION["dev_roll_gate.rebuild_completed"]
REJECTED_TOPIC: str = _BY_OPERATION["dev_roll_gate.rebuild_rejected"]
FACTS_TOPIC: str = _BY_EVENT["DevRollFacts"]
RELEASED_TOPIC: str = _BY_EVENT["DevRollReleased"]
OBSERVED_TOPIC: str = _BY_EVENT["DevRollGateObserved"]
GROUP = "onex.core-runtime.dev-roll-gate"
KEY = b"OmniNode-ai/omnimarket#dev-roll-gate"


def gate_handlers(store: InMemoryDevRollGateStore, clock: Clock) -> dict[str, Any]:
    return {
        "HandlerDevRollGateHold": HandlerDevRollGateHold(
            store, policy=POLICY, clock=clock
        ),
        "HandlerDevRollGateTick": HandlerDevRollGateTick(
            store, policy=POLICY, clock=clock
        ),
        "HandlerDevRollGateDecided": HandlerDevRollGateDecided(
            store, policy=POLICY, clock=clock
        ),
        "HandlerDevRollGateCompleted": HandlerDevRollGateCompleted(
            store, policy=POLICY, clock=clock
        ),
        "HandlerDevRollGateRejected": HandlerDevRollGateRejected(
            store, policy=POLICY, clock=clock
        ),
    }


def build(
    contract_paths: list[Path],
    handlers: dict[str, Any],
    clock: Clock,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker, frozenset[str]]:
    manifest = discover_contracts_from_paths(contract_paths)
    assert not manifest.errors, manifest.errors
    contracts = list(manifest.contracts)
    assert len(contracts) == len(contract_paths)
    topics: set[str] = set()
    for contract in contracts:
        assert contract.event_bus is not None
        topics |= set(contract.event_bus.subscribe_topics)

    def handler_resolver(ref: Any) -> Any:
        name = getattr(ref, "name", None)
        if name is None and isinstance(ref, dict):
            name = ref.get("name")
        if name in handlers:
            return handlers[name]
        raise AssertionError(f"unresolved handler ref {ref!r}")

    routing_map = build_routing_map(
        contracts, frozenset(topics), handler_resolver=handler_resolver
    )
    assert set(routing_map) == topics
    broker = InMemoryBroker(num_partitions=1)
    transport = InMemoryTransport(broker=broker, group=GROUP, topics=sorted(topics))
    dispatch = RuntimeDispatch(
        consumer=transport,
        producer=transport,
        routing_map=routing_map,
        dlq_topic_resolver=derive_canonical_dlq_topic,
        clock=lambda: clock(),
    )
    return dispatch, transport, broker, frozenset(topics)


def gate_chain(
    store: InMemoryDevRollGateStore,
    clock: Clock,
    handlers: dict[str, Any] | None = None,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker, frozenset[str]]:
    """RuntimeDispatch over the gate's five subscribed topics."""
    return build([GATE_CONTRACT], handlers or gate_handlers(store, clock), clock)


def join_chain(
    store: InMemoryDevRollGateStore,
    clock: Clock,
    compute: HandlerDevRollRequest,
    handlers: dict[str, Any] | None = None,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker, frozenset[str]]:
    """The gate and the batching node on one in-memory bus: decision requests and decisions meet."""
    return build(
        [GATE_CONTRACT, COMPUTE_CONTRACT],
        {**(handlers or gate_handlers(store, clock)), "HandlerDevRollRequest": compute},
        clock,
    )


def envelope(
    payload: dict[str, object],
    correlation_id: UUID | None = None,
    payload_type: str = "ModelDevRollHoldRequest",
) -> bytes:
    return (
        ModelEventEnvelope(
            envelope_id=uuid4(),
            payload=payload,
            correlation_id=correlation_id or uuid4(),
            event_type="golden-chain.inbound",
            payload_type=payload_type,
        )
        .model_dump_json()
        .encode("utf-8")
    )


async def send_hold(
    transport: InMemoryTransport, command: ModelDeployPublishCommand
) -> None:
    await transport.send(
        HOLD_TOPIC,
        key=KEY,
        value=envelope(
            ModelDevRollHoldRequest(command=command).model_dump(mode="json"),
            command.correlation_id,
        ),
        headers={},
    )


async def send_tick(transport: InMemoryTransport) -> None:
    await transport.send(
        TICK_TOPIC,
        key=KEY,
        value=envelope(
            ModelDevRollGateTick(tick_id=uuid4()).model_dump(mode="json"),
            payload_type="ModelDevRollGateTick",
        ),
        headers={},
    )


async def send_completed(
    transport: InMemoryTransport,
    correlation_id: UUID,
    *,
    status: str = "success",
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> None:
    payload: dict[str, object] = {
        "correlation_id": str(correlation_id),
        "status": status,
    }
    if started_at is not None:
        payload["started_at"] = started_at.isoformat()
    if completed_at is not None:
        payload["completed_at"] = completed_at.isoformat()
    await transport.send(
        COMPLETED_TOPIC,
        key=KEY,
        value=envelope(
            payload, correlation_id, payload_type="ModelDeployRebuildCompleted"
        ),
        headers={},
    )


async def send_rejected(
    transport: InMemoryTransport,
    correlation_id: UUID,
    reason: str = "busy",
    *,
    superseded_by: UUID | None = None,
) -> None:
    """A rejection as the agent writes it; a superseded one names its replacement."""
    payload: dict[str, object] = {
        "correlation_id": str(correlation_id),
        "reason": reason,
        "scope": "full",
    }
    if superseded_by is not None:
        payload["superseded_by_sha"] = "d" * 40
        payload["superseded_by_correlation_id"] = str(superseded_by)
    await transport.send(
        REJECTED_TOPIC,
        key=KEY,
        value=envelope(
            payload, correlation_id, payload_type="ModelDeployRebuildRejected"
        ),
        headers={},
    )


async def pump(dispatch: RuntimeDispatch) -> int:
    """Drain until the bus is quiet; the count of messages handled."""
    total = 0
    while (handled := await dispatch.drain()) > 0:
        total += handled
    return total


def on(broker: InMemoryBroker, topic: str) -> list[ModelEventEnvelope[object]]:
    return [
        ModelEventEnvelope[object].model_validate_json(record.value)
        for record in broker.records(topic, 0)
    ]


def released_ids(broker: InMemoryBroker) -> list[list[str]]:
    """The correlation ids of each release on the bus, in order."""
    return [
        [
            str(c.correlation_id)
            for c in ModelDevRollReleased.model_validate(m.payload).commands
        ]
        for m in on(broker, RELEASED_TOPIC)
    ]
