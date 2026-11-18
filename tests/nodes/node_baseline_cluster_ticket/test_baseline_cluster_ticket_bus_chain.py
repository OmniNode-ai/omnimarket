# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus chain of node_baseline_cluster_ticket_effect: a cluster report command on the command topic, answered on the completed topic."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
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
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_baseline_cluster_ticket_effect.adapters.adapter_inmemory_cluster_ticket_store import (
    InMemoryClusterTicketStore,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers import (
    HandlerBaselineClusterTicketBus,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.models.model_baseline_cluster_ticket import (
    ModelBaselineClusterBusOverlay,
    ModelBaselineClusterOverlayDocument,
    ModelBaselineClusterReportCommand,
)

pytestmark = pytest.mark.unit
_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_baseline_cluster_ticket_effect/contract.yaml"
)
_COMMAND_TOPIC = "onex.cmd.omnimarket.baseline-cluster-report-requested.v1"
_COMPLETED_TOPIC = "onex.evt.omnimarket.baseline-cluster-report-completed.v1"
REPO = "omnimarket"
CLUSTER = "baseline-cluster-a"


def command(**fields: object) -> ModelBaselineClusterReportCommand:
    return ModelBaselineClusterReportCommand.model_validate(
        {
            "correlation_id": str(uuid4()),
            "requested_by_lane": "lane-a",
            "repo": REPO,
            "cluster": CLUSTER,
            "failing_count": 3,
            "examples": ["test_a.py::test_x", "test_b.py::test_y"],
            "run_url": "https://ci.example.com/run/1",
            "observed_at": "2026-01-01T00:00:00Z",
            **fields,
        }
    )


def chain(
    handler: HandlerBaselineClusterTicketBus, allowlist: frozenset[str] | None = None
):
    """The in-memory RuntimeDispatch chain the contract's command topic routes to ``handler``."""
    manifest = discover_contracts_from_paths([_CONTRACT])
    assert not manifest.errors
    (contract,) = manifest.contracts
    routing = build_routing_map(
        [contract],
        allowlist if allowlist is not None else frozenset([_COMMAND_TOPIC]),
        handler_resolver=lambda _, handler=handler: handler,
    )
    broker = InMemoryBroker(num_partitions=1)
    transport = InMemoryTransport(
        broker=broker, group="baseline-cluster-ticket-bus", topics=[_COMMAND_TOPIC]
    )
    dispatch = RuntimeDispatch(
        consumer=transport,
        producer=transport,
        routing_map=routing,
        dlq_topic_resolver=derive_canonical_dlq_topic,
    )
    return routing, broker, transport, dispatch


async def send_command(
    transport: InMemoryTransport, cmd: ModelBaselineClusterReportCommand
) -> None:
    envelope = ModelEventEnvelope(
        payload=cmd.model_dump(mode="json"),
        correlation_id=cmd.correlation_id,
        payload_type="ModelBaselineClusterReportCommand",
    )
    await transport.send(
        _COMMAND_TOPIC,
        key=b"ticket",
        value=envelope.model_dump_json().encode(),
        headers={},
    )


def terminal(broker: InMemoryBroker, index: int) -> ModelEventEnvelope[object]:
    records = broker.records(_COMPLETED_TOPIC, 0)
    return ModelEventEnvelope[object].model_validate_json(records[index].value)


def overlay() -> ModelBaselineClusterBusOverlay:
    return ModelBaselineClusterBusOverlay(
        timeout_seconds=5,
        ticket_store="inmemory",
        parent="OMN-19998",
        gate="dev-baseline",
        diagnosis="example-org/example-repo#1",
        max_duplicate_hops=3,
        example_limit=5,
    )


async def test_golden_chain_bus_report_creates_ticket():
    store = InMemoryClusterTicketStore()
    handler = HandlerBaselineClusterTicketBus(
        overlay_loader=overlay, store_factory=lambda *_: store
    )
    routing, broker, transport, dispatch = chain(handler)
    assert set(routing) == {_COMMAND_TOPIC}

    first = command()
    await send_command(transport, first)
    assert await dispatch.drain() == 1
    records = broker.records(_COMPLETED_TOPIC, 0)
    assert len(records) == 1
    done = terminal(broker, 0)
    assert done.correlation_id == first.correlation_id
    assert done.payload["correlation_id"] == str(first.correlation_id)
    assert done.payload["requested_by_lane"] == first.requested_by_lane
    assert done.payload["repo"] == REPO
    assert done.payload["cluster"] == CLUSTER
    assert done.payload["outcome"] == "completed"
    assert done.payload["result"]["state"] == "created"
    assert done.payload["result"]["ticket"]

    replay = command(correlation_id=uuid4())
    await send_command(transport, replay)
    assert await dispatch.drain() == 1
    done = terminal(broker, 1)
    assert done.correlation_id == replay.correlation_id
    assert done.payload["result"]["state"] == "commented"
    assert (
        done.payload["result"]["ticket"]
        == terminal(broker, 0).payload["result"]["ticket"]
    )
    assert len(store.tickets) == 1


async def test_error_chain_missing_overlay_is_typed_unavailable_on_the_completed_topic():
    def missing_overlay() -> ModelBaselineClusterBusOverlay:
        raise NodeOverlayError(
            "overlay is invalid for node_baseline_cluster_ticket_effect "
            "(ValidationError): fields ['baseline_cluster.timeout_seconds']"
        )

    handler = HandlerBaselineClusterTicketBus(
        overlay_loader=missing_overlay,
        store_factory=lambda *_: InMemoryClusterTicketStore(),
    )
    _, broker, transport, dispatch = chain(handler)
    cmd = command()
    await send_command(transport, cmd)
    assert await dispatch.drain() == 1
    records = broker.records(_COMPLETED_TOPIC, 0)
    assert len(records) == 1
    done = terminal(broker, 0)
    assert done.correlation_id == cmd.correlation_id
    assert done.payload["outcome"] == "completed"
    assert done.payload["result"]["state"] == "unavailable"
    assert done.payload["result"]["cause"].startswith("overlay-unavailable:")
    assert "baseline_cluster.timeout_seconds" in done.payload["result"]["cause"]


async def test_error_chain_store_failure_is_typed_unavailable():
    def failing_factory(kind: str, lane: str, timeout_seconds: float):
        raise RuntimeError("secret-value-in-text")

    handler = HandlerBaselineClusterTicketBus(
        overlay_loader=overlay, store_factory=failing_factory
    )
    _, broker, transport, dispatch = chain(handler)
    cmd = command()
    await send_command(transport, cmd)
    assert await dispatch.drain() == 1
    records = broker.records(_COMPLETED_TOPIC, 0)
    assert len(records) == 1
    done = terminal(broker, 0)
    assert done.payload["result"]["state"] == "unavailable"
    assert done.payload["result"]["cause"] == "store-unavailable:RuntimeError"
    assert b"secret-value-in-text" not in records[0].value


@pytest.mark.parametrize(
    "key",
    [
        "timeout_seconds",
        "ticket_store",
        "parent",
        "gate",
        "diagnosis",
        "max_duplicate_hops",
        "example_limit",
    ],
)
async def test_every_overlay_key_is_required(
    key: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = {
        "baseline_cluster": {
            "timeout_seconds": 5,
            "ticket_store": "inmemory",
            "parent": "OMN-19998",
            "gate": "dev-baseline",
            "diagnosis": "example-org/example-repo#1",
            "max_duplicate_hops": 3,
            "example_limit": 5,
        }
    }
    del document["baseline_cluster"][key]
    with pytest.raises(ValidationError) as excinfo:
        ModelBaselineClusterOverlayDocument.model_validate(document)
    assert key in str(excinfo.value)

    handler = HandlerBaselineClusterTicketBus()
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    result = await handler.handle(command())
    assert result.result.state == "unavailable"
    assert result.result.cause is not None
    assert result.result.cause.startswith("overlay-unavailable:")


def test_contract_declares_the_bus_route() -> None:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert data["event_bus"]["subscribe_topics"] == [_COMMAND_TOPIC]
    assert data["event_bus"]["publish_topics"] == [_COMPLETED_TOPIC]
    assert data["terminal_event"] == _COMPLETED_TOPIC
    assert data["runtime_dispatch"]["command_topic"] == _COMMAND_TOPIC
