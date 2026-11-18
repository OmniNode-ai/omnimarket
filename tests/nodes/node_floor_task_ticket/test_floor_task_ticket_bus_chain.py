# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus chain of node_floor_task_ticket_effect: a mint command on the command topic, answered on the completed topic."""

from __future__ import annotations

import importlib
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
from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_inmemory_floor_ticket_store import (
    InMemoryFloorTicketStore,
)
from omnimarket.nodes.node_floor_task_ticket_effect.handlers import (
    HandlerFloorTaskTicketMintBus,
)
from omnimarket.nodes.node_floor_task_ticket_effect.models import (
    ModelFloorTaskTicketBusOverlay,
    ModelFloorTaskTicketMintCommand,
    ModelFloorTaskTicketMintCompleted,
    ModelFloorTaskTicketOverlayDocument,
)

pytestmark = pytest.mark.unit
_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_floor_task_ticket_effect/contract.yaml"
)
_COMMAND_TOPIC = "onex.cmd.omnimarket.floor-task-ticket-mint-requested.v1"
_COMPLETED_TOPIC = "onex.evt.omnimarket.floor-task-ticket-mint-completed.v1"
FLOOR_KEY = "floor:bus:line-a"


def command(**fields: object) -> ModelFloorTaskTicketMintCommand:
    return ModelFloorTaskTicketMintCommand.model_validate(
        {
            "correlation_id": str(uuid4()),
            "requested_by_lane": "lane-a",
            "floor_key": FLOOR_KEY,
            "title": "T1. The bus mint",
            "description": f"Floor key: {FLOOR_KEY}",
            "parent": "OMN-20001",
            **fields,
        }
    )


def chain(
    handler: HandlerFloorTaskTicketMintBus, allowlist: frozenset[str] | None = None
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
        broker=broker, group="floor-task-ticket-bus", topics=[_COMMAND_TOPIC]
    )
    dispatch = RuntimeDispatch(
        consumer=transport,
        producer=transport,
        routing_map=routing,
        dlq_topic_resolver=derive_canonical_dlq_topic,
    )
    return routing, broker, transport, dispatch


async def send_command(
    transport: InMemoryTransport, cmd: ModelFloorTaskTicketMintCommand
) -> None:
    envelope = ModelEventEnvelope(
        payload=cmd.model_dump(mode="json"),
        correlation_id=cmd.correlation_id,
        payload_type="ModelFloorTaskTicketMintCommand",
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


def overlay() -> ModelFloorTaskTicketBusOverlay:
    return ModelFloorTaskTicketBusOverlay(
        mint_timeout_seconds=5, ticket_store="inmemory"
    )


async def test_golden_chain_bus_mint_returns_ticket():
    store = InMemoryFloorTicketStore()
    handler = HandlerFloorTaskTicketMintBus(
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
    assert done.payload["floor_key"] == FLOOR_KEY
    assert done.payload["outcome"] == "completed"
    assert done.payload["result"]["state"] == "ticket"
    assert done.payload["result"]["ticket"]
    assert not done.payload["result"]["reused"]

    replay = command(correlation_id=uuid4())
    await send_command(transport, replay)
    assert await dispatch.drain() == 1
    done = terminal(broker, 1)
    assert done.correlation_id == replay.correlation_id
    assert done.payload["result"]["state"] == "ticket"
    assert done.payload["result"]["reused"]
    assert (
        done.payload["result"]["ticket"]
        == terminal(broker, 0).payload["result"]["ticket"]
    )
    assert len(store.tickets) == 1


async def test_error_chain_missing_overlay_is_typed_unavailable_on_the_completed_topic():
    def missing_overlay() -> ModelFloorTaskTicketBusOverlay:
        raise NodeOverlayError(
            "overlay is invalid for node_floor_task_ticket_effect "
            "(ValidationError): fields ['floor_fill.mint_timeout_seconds']"
        )

    handler = HandlerFloorTaskTicketMintBus(
        overlay_loader=missing_overlay,
        store_factory=lambda *_: InMemoryFloorTicketStore(),
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
    assert "floor_fill.mint_timeout_seconds" in done.payload["result"]["cause"]


async def test_error_chain_store_failure_is_typed_unavailable():
    def failing_factory(kind: str, lane: str, timeout_seconds: float):
        raise RuntimeError("secret-value-in-text")

    handler = HandlerFloorTaskTicketMintBus(
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


async def test_error_chain_gate_refusal_is_typed_refused():
    store = InMemoryFloorTicketStore(
        gate=lambda *_: "unfalsified_criterion,missing_gate_line"
    )
    handler = HandlerFloorTaskTicketMintBus(
        overlay_loader=overlay, store_factory=lambda *_: store
    )
    _, broker, transport, dispatch = chain(handler)
    await send_command(transport, command())
    assert await dispatch.drain() == 1
    done = terminal(broker, 0)
    assert done.payload["result"]["state"] == "refused"
    assert done.payload["result"]["cause"].startswith("ticket-refused:")
    assert not store.tickets


@pytest.mark.parametrize("key", ["mint_timeout_seconds", "ticket_store"])
async def test_every_overlay_key_is_required(
    key: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = {"floor_fill": {"mint_timeout_seconds": 5, "ticket_store": "inmemory"}}
    del document["floor_fill"][key]
    with pytest.raises(ValidationError) as excinfo:
        ModelFloorTaskTicketOverlayDocument.model_validate(document)
    assert key in str(excinfo.value)

    handler = HandlerFloorTaskTicketMintBus()
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    result = await handler.handle(command())
    assert result.result.state == "unavailable"
    assert result.result.cause is not None
    assert result.result.cause.startswith("overlay-unavailable:")


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.pop("correlation_id"), id="correlation-id"),
        pytest.param(lambda p: p.update(requested_by_lane="lane a"), id="lane"),
        pytest.param(lambda p: p.update(parent="TICKET-1"), id="parent"),
    ],
)
def test_command_requires_correlation_and_lane(mutate) -> None:
    payload = command().model_dump(mode="json")
    mutate(payload)
    with pytest.raises(ValidationError):
        ModelFloorTaskTicketMintCommand.model_validate(payload)


def test_contract_declares_the_bus_route() -> None:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert data["event_bus"]["subscribe_topics"] == [_COMMAND_TOPIC]
    assert data["event_bus"]["publish_topics"] == [_COMPLETED_TOPIC]
    assert data["terminal_event"] == _COMPLETED_TOPIC
    assert data["runtime_dispatch"]["command_topic"] == _COMMAND_TOPIC

    (route,) = data["handler_routing"]["handlers"]
    handler_cls = getattr(
        importlib.import_module(route["handler"]["module"]), route["handler"]["name"]
    )
    assert handler_cls is HandlerFloorTaskTicketMintBus
    for section, expected in [
        ("input_model", ModelFloorTaskTicketMintCommand),
        ("output_model", ModelFloorTaskTicketMintCompleted),
    ]:
        assert (
            getattr(
                importlib.import_module(data[section]["module"]), data[section]["name"]
            )
            is expected
        )
    assert (
        getattr(
            importlib.import_module(route["event_model"]["module"]),
            route["event_model"]["name"],
        )
        is ModelFloorTaskTicketMintCommand
    )
    assert data["descriptor"]["runtime_profiles"] == ["effects"]


async def test_control_no_route_no_record():
    """A command topic no route owns never reaches a handler, so no completed record is emitted."""
    handler = HandlerFloorTaskTicketMintBus(
        overlay_loader=overlay,
        store_factory=lambda *_: InMemoryFloorTicketStore(),
    )
    routing, broker, transport, dispatch = chain(handler, allowlist=frozenset())
    assert routing == {}
    await send_command(transport, command())
    await dispatch.drain()
    assert list(broker.records(_COMPLETED_TOPIC, 0)) == []
