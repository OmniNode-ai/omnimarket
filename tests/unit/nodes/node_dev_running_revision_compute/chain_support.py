# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The golden and error chain harness: the real contract, the runtime's own dispatch, the in-memory bus."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
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

from omnimarket.nodes.node_dev_running_revision_compute.handlers import (
    HandlerDevRunningRevision,
)
from omnimarket.nodes.node_dev_running_revision_compute.models import (
    ModelRunningRevisionPolicy,
)

ROOT = Path(__file__).resolve().parents[4]
CONTRACT = ROOT / "src/omnimarket/nodes/node_dev_running_revision_compute/contract.yaml"
_DISPATCH = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["runtime_dispatch"]
# The topics are read from the contract, the single place the node declares them.
COMMAND_TOPIC: str = _DISPATCH["command_topic"]
READ_TOPIC: str = _DISPATCH["terminal_events"]["success"]
REFUSED_TOPIC: str = _DISPATCH["terminal_events"]["failure"]
GROUP = "onex.core-runtime.dev-running-revision"
CLOCK = datetime(2026, 11, 12, 16, 0, 0, tzinfo=UTC)
KEY = b"OmniNode-ai/omnimarket#dev-running-revision"


def chain(
    policy: ModelRunningRevisionPolicy,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker, frozenset[str]]:
    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors, manifest.errors
    (contract,) = manifest.contracts
    assert contract.event_bus is not None
    topics = frozenset(contract.event_bus.subscribe_topics)
    handler = HandlerDevRunningRevision(policy)
    routing_map = build_routing_map(
        [contract], topics, handler_resolver=lambda _ref: handler
    )
    assert set(routing_map) == topics
    broker = InMemoryBroker(num_partitions=1)
    transport = InMemoryTransport(broker=broker, group=GROUP, topics=sorted(topics))
    dispatch = RuntimeDispatch(
        consumer=transport,
        producer=transport,
        routing_map=routing_map,
        dlq_topic_resolver=derive_canonical_dlq_topic,
        clock=lambda: CLOCK,
    )
    return dispatch, transport, broker, topics


def envelope(payload: dict[str, object], correlation_id: UUID) -> bytes:
    return (
        ModelEventEnvelope(
            envelope_id=uuid4(),
            payload=payload,
            correlation_id=correlation_id,
            event_type="golden-chain.inbound",
            payload_type="ModelRunningRevisionFacts",
        )
        .model_dump_json()
        .encode("utf-8")
    )


def on(broker: InMemoryBroker, topic: str) -> list[ModelEventEnvelope[object]]:
    return [
        ModelEventEnvelope[object].model_validate_json(record.value)
        for record in broker.records(topic, 0)
    ]
