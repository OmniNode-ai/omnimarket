# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The golden and error chain harness: the real contract, the runtime's own dispatch, the in-memory bus."""

from __future__ import annotations

from datetime import UTC, datetime
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

from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers import (
    HandlerDevRollIncidentCapture,
    HandlerDevRollManifestRecord,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers.store_dev_roll_artifact import (
    InMemoryDevRollArtifactStore,
)
from tests.unit.nodes.node_dev_roll_failure_capture_effect.conftest import POLICY

ROOT = Path(__file__).resolve().parents[4]
CONTRACT = (
    ROOT / "src/omnimarket/nodes/node_dev_roll_failure_capture_effect/contract.yaml"
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
MANIFEST_TOPIC: str = _BY_OPERATION["dev_roll.record_manifest"]
INCIDENT_TOPIC: str = _BY_OPERATION["dev_roll.capture_incident"]
RECORDED_TOPIC: str = _BY_EVENT["DevRollManifestRecorded"]
TERMINAL_TOPIC: str = _BY_EVENT["DevRollTerminal"]
GROUP = "onex.core-runtime.dev-roll-failure-capture"
CLOCK = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)
KEY = b"OmniNode-ai/omnimarket#dev-roll-failure-capture"


def chain(
    store: InMemoryDevRollArtifactStore,
    clock: datetime = CLOCK,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker, frozenset[str]]:
    """RuntimeDispatch over BOTH subscribed topics, the two handlers sharing one store."""
    return chain_with(
        HandlerDevRollManifestRecord(store, policy=POLICY),
        HandlerDevRollIncidentCapture(store, policy=POLICY, clock=lambda: clock),
        clock,
    )


def chain_with(
    manifest_handler: HandlerDevRollManifestRecord,
    incident_handler: HandlerDevRollIncidentCapture,
    clock: datetime = CLOCK,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker, frozenset[str]]:
    """The same real harness over the two handlers the caller built."""
    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors, manifest.errors
    (contract,) = manifest.contracts
    assert contract.event_bus is not None
    topics = frozenset(contract.event_bus.subscribe_topics)

    def handler_resolver(ref: Any) -> Any:
        name = getattr(ref, "name", None)
        if name is None and isinstance(ref, dict):
            name = ref.get("name")
        if name == "HandlerDevRollManifestRecord":
            return manifest_handler
        if name == "HandlerDevRollIncidentCapture":
            return incident_handler
        raise AssertionError(f"unresolved handler ref {ref!r}")

    routing_map = build_routing_map(
        [contract], topics, handler_resolver=handler_resolver
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
    return dispatch, transport, broker, topics


def envelope(payload: dict[str, object], correlation_id: UUID) -> bytes:
    return (
        ModelEventEnvelope(
            envelope_id=uuid4(),
            payload=payload,
            correlation_id=correlation_id,
            event_type="golden-chain.inbound",
            payload_type="ModelDevRollIncidentEvent",
        )
        .model_dump_json()
        .encode("utf-8")
    )


def on(broker: InMemoryBroker, topic: str) -> list[ModelEventEnvelope[object]]:
    return [
        ModelEventEnvelope[object].model_validate_json(record.value)
        for record in broker.records(topic, 0)
    ]
