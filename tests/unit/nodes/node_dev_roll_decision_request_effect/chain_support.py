# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The golden and error chain harness: the real contracts, the runtime's own dispatch, the in-memory bus."""

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

from omnimarket.nodes.node_dev_roll_decision_request_effect.handlers import (
    HandlerDevRollDecidedObserved,
    HandlerDevRollQueuedRoll,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.handlers.store_dev_roll_observation import (
    InMemoryDevRollObservationStore,
)
from omnimarket.nodes.node_dev_roll_request_compute.handlers import (
    HandlerDevRollRequest,
)
from tests.unit.nodes.node_dev_roll_decision_request_effect.conftest import (
    NOW,
    POLICY,
)

ROOT = Path(__file__).resolve().parents[4]
CONTRACT = (
    ROOT / "src/omnimarket/nodes/node_dev_roll_decision_request_effect/contract.yaml"
)
COMPUTE_CONTRACT = (
    ROOT / "src/omnimarket/nodes/node_dev_roll_request_compute/contract.yaml"
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
MANIFEST_TOPIC: str = _BY_OPERATION["dev_roll_decision_request.queued_roll"]
DECIDED_TOPIC: str = _BY_OPERATION["dev_roll_decision_request.decided_observed"]
FACTS_TOPIC: str = _BY_EVENT["DevRollFacts"]
SKIPPED_TOPIC: str = _BY_EVENT["DevRollDecisionRequestSkipped"]
RECORDED_TOPIC: str = _BY_EVENT["DevRollObservationRecorded"]
GROUP = "onex.core-runtime.dev-roll-decision-request"
KEY = b"OmniNode-ai/omnimarket#dev-roll-decision-request"


def _build(
    contract_paths: list[Path],
    handlers: dict[str, Any],
    clock: datetime,
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
        clock=lambda: clock,
    )
    return dispatch, transport, broker, frozenset(topics)


def chain(
    store: InMemoryDevRollObservationStore,
    clock: datetime = NOW,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker, frozenset[str]]:
    """RuntimeDispatch over this node's two subscribed topics, the two handlers sharing one store."""
    return chain_with(
        HandlerDevRollQueuedRoll(store, policy=POLICY, clock=lambda: clock),
        HandlerDevRollDecidedObserved(store, policy=POLICY),
        clock,
    )


def chain_with(
    queued: HandlerDevRollQueuedRoll,
    observed: HandlerDevRollDecidedObserved,
    clock: datetime = NOW,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker, frozenset[str]]:
    """The same real harness over the two handlers the caller built."""
    return _build(
        [CONTRACT],
        {"HandlerDevRollQueuedRoll": queued, "HandlerDevRollDecidedObserved": observed},
        clock,
    )


def join_chain(
    store: InMemoryDevRollObservationStore,
    compute_handler: HandlerDevRollRequest,
    clock: datetime = NOW,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker, frozenset[str]]:
    """Both nodes' real contracts on one in-memory bus: the join of the request effect and the batching compute node."""
    return _build(
        [CONTRACT, COMPUTE_CONTRACT],
        {
            "HandlerDevRollQueuedRoll": HandlerDevRollQueuedRoll(
                store, policy=POLICY, clock=lambda: clock
            ),
            "HandlerDevRollDecidedObserved": HandlerDevRollDecidedObserved(
                store, policy=POLICY
            ),
            "HandlerDevRollRequest": compute_handler,
        },
        clock,
    )


def envelope(
    payload: dict[str, object],
    correlation_id: UUID | None = None,
    payload_type: str = "ModelDevRollManifestEvent",
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
