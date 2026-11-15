# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The golden and error chain harness: the real contract, the runtime's own dispatch, the in-memory bus."""

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

from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers import (
    HandlerDevRollWindowCompleted,
    HandlerDevRollWindowHold,
    HandlerDevRollWindowTick,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.conftest import Clock, Gate

ROOT = Path(__file__).resolve().parents[4]
CONTRACT = ROOT / "src/omnimarket/nodes/node_dev_roll_window_gate_effect/contract.yaml"
DEPLOY_CONTRACT = (
    ROOT / "src/omnimarket/nodes/node_redeploy_deploy_effect/contract.yaml"
)
_CONTRACT = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
# The topics are read from the contract, the single place the node declares them.
_BY_OPERATION: dict[str, str] = {
    entry["operation"]: entry["topic"]
    for entry in _CONTRACT["handler_routing"]["handlers"]
}
_BY_EVENT: dict[str, str] = {
    entry["event_type"]: entry["topic"] for entry in _CONTRACT["published_events"]
}
HOLD_TOPIC: str = _BY_OPERATION["dev_roll_window.hold"]
TICK_TOPIC: str = _BY_OPERATION["dev_roll_window.tick"]
COMPLETED_TOPIC: str = _BY_OPERATION["dev_roll_window.completed"]
DEPLOY_PUBLISH_TOPIC: str = _BY_EVENT["ModelDeployPublishCommand"]
HELD_TOPIC: str = _BY_EVENT["ModelDevRollGateHeld"]
REFUSED_TOPIC: str = _BY_EVENT["ModelDevRollGateRefused"]
DECISION_TOPIC: str = _BY_EVENT["ModelDevRollGateDecision"]
RECORDED_TOPIC: str = _BY_EVENT["ModelDevRollGateRollRecorded"]
GROUP = "onex.core-runtime.dev-roll-window-gate"
KEY = b"OmniNode-ai/omnimarket#dev-roll-window-gate"


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
        clock=lambda: clock.now,
    )
    return dispatch, transport, broker, frozenset(topics)


def chain(
    gate: Gate,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker, frozenset[str]]:
    """RuntimeDispatch over this node's three subscribed topics, the handlers of one Gate."""
    return chain_with(gate.hold, gate.tick, gate.completed, gate.clock)


def chain_with(
    hold: HandlerDevRollWindowHold,
    tick: HandlerDevRollWindowTick,
    completed: HandlerDevRollWindowCompleted,
    clock: Clock,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker, frozenset[str]]:
    """The same real harness over the three handlers the caller built."""
    return build(
        [CONTRACT],
        {
            "HandlerDevRollWindowHold": hold,
            "HandlerDevRollWindowTick": tick,
            "HandlerDevRollWindowCompleted": completed,
        },
        clock,
    )


def envelope(
    payload: dict[str, object],
    payload_type: str,
    correlation_id: UUID | None = None,
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


def on(broker: InMemoryBroker, topic: str) -> list[ModelEventEnvelope[object]]:
    return [
        ModelEventEnvelope[object].model_validate_json(record.value)
        for record in broker.records(topic, 0)
    ]


def tick_payload(now: datetime) -> dict[str, object]:
    return {
        "now": now.isoformat(),
        "tick_id": str(uuid4()),
        "sequence_number": 1,
        "scheduled_at": now.isoformat(),
        "correlation_id": str(uuid4()),
        "scheduler_id": "test",
        "tick_interval_ms": 1000,
    }
