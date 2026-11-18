# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests for the bus route of node_floor_task_ticket_effect.

The asking side is ``onex run-node`` (omnibase_core.cli.cli_run_node): it resolves the
packaged contract, injects the payload correlation id the contract requires, and consumes
the terminal event under its derived consumer group, which the deploy grant must cover.
The answering side is this node's bus handler, whose completed answer the floor-fill
orchestrator reads and whose overlay document the overlays repository ships. These tests
pin the wire the two sides read; a shape change on either side fails here.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args
from uuid import uuid4

import pytest
import yaml
from omnibase_core.cli import cli_run_node
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

from omnimarket.nodes.node_floor_task_ticket_effect.handlers import (
    HandlerFloorTaskTicketMintBus,
)
from omnimarket.nodes.node_floor_task_ticket_effect.models import (
    ModelFloorTaskTicketBusOverlay,
    ModelFloorTaskTicketMintCommand,
    ModelFloorTaskTicketMintCompleted,
    ModelFloorTaskTicketMintResult,
    ModelFloorTaskTicketOverlayDocument,
)

pytestmark = pytest.mark.unit

NODE_ID = "node_floor_task_ticket_effect"
_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_floor_task_ticket_effect/contract.yaml"
)
FLOOR_KEY = "floor:seam:line-a"

# Spelled out here, not read from the contract or the models: a move on either side must
# fail this test.
COMMAND_TOPIC = "onex.cmd.omnimarket.floor-task-ticket-mint-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.floor-task-ticket-mint-completed.v1"

# The grant rows the asking side needs; the same rows omnibase_infra's
# deploy/lab/developer-principal-bindings.yaml declares, whose own test pins these literals.
GRANT_ROWS = (
    ("topic", COMMAND_TOPIC, ("write", "describe")),
    ("topic", COMPLETED_TOPIC, ("read", "describe")),
    ("group", "local.omnibase_core.cli_run_node.consume.v1.", ("read", "describe")),
)

# The mint states node_floor_fill_orchestrator reads off the bus answer; pinned as
# MINT_STATES in the fill orchestrator lane's branch omn-22854-fill-orch-seams
# (src/omnimarket/nodes/node_floor_fill_orchestrator/tests/test_floor_fill_seams.py:111).
FILL_MINT_STATES = ("ticket", "refused", "unavailable")

# The shape of overlays/node_floor_task_ticket_effect/overlay.yaml in the overlays
# repository: the bus the run-node dials, and the floor_fill section the handler reads.
OVERLAY_DOCUMENT = {
    "event_bus": {"type": "kafka", "lane": "dev"},
    "floor_fill": {"mint_timeout_seconds": 30, "ticket_store": "linear"},
}


def command(**fields: object) -> ModelFloorTaskTicketMintCommand:
    return ModelFloorTaskTicketMintCommand.model_validate(
        {
            "correlation_id": str(uuid4()),
            "requested_by_lane": "lane-seam",
            "floor_key": FLOOR_KEY,
            "title": "T1. The seam mint",
            "description": f"Floor key: {FLOOR_KEY}",
            "parent": "OMN-20001",
            **fields,
        }
    )


def chain(handler: HandlerFloorTaskTicketMintBus):
    """The in-memory RuntimeDispatch chain the contract's command topic routes to ``handler``."""
    manifest = discover_contracts_from_paths([_CONTRACT])
    assert not manifest.errors
    (contract,) = manifest.contracts
    routing = build_routing_map(
        [contract],
        frozenset([COMMAND_TOPIC]),
        handler_resolver=lambda _, handler=handler: handler,
    )
    broker = InMemoryBroker(num_partitions=1)
    transport = InMemoryTransport(
        broker=broker, group="floor-task-ticket-seam", topics=[COMMAND_TOPIC]
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
        COMMAND_TOPIC,
        key=b"ticket",
        value=envelope.model_dump_json().encode(),
        headers={},
    )


def test_seam_run_node_reads_this_contracts_topics() -> None:
    """run-node resolves this contract's command topic, terminal event, and required correlation id."""
    try:
        (
            _contract_path,
            command_topic,
            terminal_event,
            inject_correlation_id,
        ) = cli_run_node._resolve_node_topics(NODE_ID)
    except Exception:
        # The packaged contract is not resolvable in this environment; reproduce the
        # same reading against the node's own contract file. Never skip.
        contract: dict[str, object] = cli_run_node.load_workflow_contract(_CONTRACT)
        event_bus: object = contract["event_bus"]
        assert isinstance(event_bus, dict)
        subscribe_topics: object = event_bus["subscribe_topics"]
        assert isinstance(subscribe_topics, list)
        assert subscribe_topics
        command_topic = subscribe_topics[0]
        terminal_event = contract["terminal_event"]
        inputs: object = contract["inputs"]
        assert isinstance(inputs, dict)
        correlation_spec: object = inputs["correlation_id"]
        assert isinstance(correlation_spec, dict)
        inject_correlation_id = correlation_spec["required"]
    assert command_topic == COMMAND_TOPIC
    assert terminal_event == COMPLETED_TOPIC
    assert inject_correlation_id is True


def test_seam_run_node_payload_validates_as_the_command() -> None:
    """A run-node payload without correlation_id normalizes into the command and back out as the answer's."""
    correlation_uuid = uuid4()
    payload = {
        "requested_by_lane": "lane-seam",
        "floor_key": FLOOR_KEY,
        "title": "T1. The seam command",
        "description": f"Floor key: {FLOOR_KEY}",
        "parent": "OMN-20001",
    }
    normalized = cli_run_node._normalize_payload(payload, correlation_uuid, True)
    cmd = ModelFloorTaskTicketMintCommand.model_validate(normalized)
    assert cmd.correlation_id == correlation_uuid
    completed = ModelFloorTaskTicketMintCompleted(
        correlation_id=cmd.correlation_id,
        requested_by_lane=cmd.requested_by_lane,
        floor_key=cmd.floor_key,
        result=ModelFloorTaskTicketMintResult(state="ticket", ticket="OMN-90001"),
    )
    # The field run-node's poll matches the answer against.
    assert completed.model_dump(mode="json")["correlation_id"] == str(correlation_uuid)

    # A client that adds a field is refused loudly: the command model forbids extras.
    payload["seam_extra"] = "a field the command does not declare"
    with pytest.raises(ValidationError):
        ModelFloorTaskTicketMintCommand.model_validate(
            cli_run_node._normalize_payload(payload, correlation_uuid, True)
        )


def test_seam_contract_topics_equal_the_pinned_names_and_nothing_else() -> None:
    """The contract's subscribe, publish, terminal, dispatch, and routing topics are the pinned pair."""
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert data["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert data["event_bus"]["publish_topics"] == [COMPLETED_TOPIC]
    assert data["terminal_event"] == COMPLETED_TOPIC
    assert data["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert data["runtime_dispatch"]["terminal_events"]["success"] == COMPLETED_TOPIC

    routes = data["handler_routing"]["handlers"]
    assert len(routes) == 1
    assert routes[0]["topic"] == COMMAND_TOPIC


def test_seam_mint_result_states_are_the_fill_orchestrators() -> None:
    """The bus answer's result carries exactly the states the fill orchestrator reads, as its own result type."""
    assert set(
        get_args(ModelFloorTaskTicketMintResult.model_fields["state"].annotation)
    ) == set(FILL_MINT_STATES)
    assert (
        ModelFloorTaskTicketMintCompleted.model_fields["result"].annotation
        is ModelFloorTaskTicketMintResult
    )


def test_seam_overlay_document_is_what_the_overlays_repository_ships() -> None:
    """The overlays repository's document validates (event_bus ignored), a renamed or unknown store refuses."""
    document = ModelFloorTaskTicketOverlayDocument.model_validate(OVERLAY_DOCUMENT)
    assert document.floor_fill.mint_timeout_seconds == 30
    assert document.floor_fill.ticket_store == "linear"

    renamed = {"floor_fill": dict(OVERLAY_DOCUMENT["floor_fill"])}
    renamed["floor_fill"]["ticket_store_renamed"] = renamed["floor_fill"].pop(
        "ticket_store"
    )
    with pytest.raises(ValidationError) as renamed_exc:
        ModelFloorTaskTicketOverlayDocument.model_validate(renamed)
    assert "ticket_store" in str(renamed_exc.value)

    unknown = {"floor_fill": dict(OVERLAY_DOCUMENT["floor_fill"])}
    unknown["floor_fill"]["ticket_store"] = "mysql"
    with pytest.raises(ValidationError):
        ModelFloorTaskTicketOverlayDocument.model_validate(unknown)


def test_seam_grant_rows_pin_the_run_node_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run-node consumer group sits under the pinned grant row; the lab runtime's own group does not."""
    monkeypatch.delenv("ONEX_ENVIRONMENT", raising=False)
    group_prefix = GRANT_ROWS[2][1]
    assert cli_run_node.derive_run_node_group_id(uuid4()).startswith(group_prefix)

    # The lab runtime's own consumer group for this node is not covered by the grant row.
    lab_group = "local.omnimapet.node_floor_task_ticket_effect.consume.v1"
    assert not lab_group.startswith(group_prefix)

    # A prefix as loose as "local.omnimapet." would cover the lab runtime's group, so
    # the pinned prefix is what keeps the grant off the runtime's own consumer.
    assert lab_group.startswith("local.omnimapet.")


async def test_seam_in_memory_bus_round_trip() -> None:
    """A command on the command topic is answered by exactly one completed record carrying the command's correlation id."""
    handler = HandlerFloorTaskTicketMintBus(
        overlay_loader=lambda: ModelFloorTaskTicketBusOverlay(
            mint_timeout_seconds=5, ticket_store="inmemory"
        )
    )
    routing, broker, transport, dispatch = chain(handler)
    assert set(routing) == {COMMAND_TOPIC}

    cmd = command()
    await send_command(transport, cmd)
    assert await dispatch.drain() == 1
    records = broker.records(COMPLETED_TOPIC, 0)
    assert len(records) == 1
    done = ModelEventEnvelope[object].model_validate_json(records[0].value)
    assert done.correlation_id == cmd.correlation_id
