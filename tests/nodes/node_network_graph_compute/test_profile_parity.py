# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC5: the in-memory profile and the Redpanda profile give equal output digests.

Both profiles run the same step on the same request bytes: decode the request, run
HandlerNetworkGraphCompute, encode the document. Only the transport differs, an
in-memory bus or a Redpanda broker, so the digests can differ only if the node's
output depends on the profile. The topics are the contract's, read from it.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.network_graph import ModelNetworkGraphRequest
from omnimarket.nodes.node_network_graph_compute.handlers.handler_network_graph_compute import (
    HandlerNetworkGraphCompute,
)

from .support import bus_text, contract, node_text, request

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_network_graph_compute/contract.yaml"
)
_TIMEOUT_SECONDS = 30


def _topics() -> tuple[str, str]:
    declared = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return declared["runtime_dispatch"]["command_topic"], declared["terminal_event"]


def _request_bytes() -> bytes:
    return (
        request(
            contract("repo_a", "n/pub/contract.yaml", bus_text("pub", ("t.fan",), ())),
            contract("repo_a", "n/sub/contract.yaml", bus_text("sub", (), ("t.fan",))),
            contract(
                "repo_b",
                "n/pub/contract.yaml",
                bus_text("pub", ("t.other",), ("t.fan",)),
            ),
            contract(
                "repo_a", "n/bad/contract.yaml", node_text("bad", "event_bus: [open\n")
            ),
            contract("repo_a", "n/none/contract.yaml", "name: not_a_node\n"),
        )
        .model_dump_json()
        .encode("utf-8")
    )


def node_step(payload: bytes) -> bytes:
    """The node's work, identical under every profile."""
    document = HandlerNetworkGraphCompute().handle(
        ModelNetworkGraphRequest.model_validate_json(payload)
    )
    return document.model_dump_json().encode("utf-8")


def _digest(document_bytes: bytes) -> str:
    return hashlib.sha256(document_bytes).hexdigest()


def _value(message: object) -> bytes:
    return bytes(getattr(message, "value", b""))


async def run_in_memory(payload: bytes) -> bytes:
    command, terminal = _topics()
    bus = EventBusInmemory(environment="local", group="parity")
    await bus.start()
    done: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)

    async def on_command(message: object) -> None:
        await bus.publish(terminal, None, node_step(_value(message)), None)

    async def on_terminal(message: object) -> None:
        await done.put(_value(message))

    try:
        await bus.subscribe(command, None, on_command, group_id="node")
        await bus.subscribe(terminal, None, on_terminal, group_id="reader")
        await bus.publish(command, None, payload, None)
        return await asyncio.wait_for(done.get(), _TIMEOUT_SECONDS)
    finally:
        await bus.close()


async def run_redpanda(bootstrap: str, payload: bytes) -> bytes:
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    command, terminal = _topics()
    run = uuid.uuid4().hex[:8]
    command, terminal = f"{command}.parity-{run}", f"{terminal}.parity-{run}"
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
    await producer.start()
    try:
        await producer.send_and_wait(command, payload)
        worker = AIOKafkaConsumer(
            command,
            bootstrap_servers=bootstrap,
            group_id=f"node-{run}",
            auto_offset_reset="earliest",
        )
        await worker.start()
        try:
            received = await asyncio.wait_for(worker.getone(), _TIMEOUT_SECONDS)
            await producer.send_and_wait(
                terminal, node_step(bytes(received.value or b""))
            )
        finally:
            await worker.stop()
        reader = AIOKafkaConsumer(
            terminal,
            bootstrap_servers=bootstrap,
            group_id=f"reader-{run}",
            auto_offset_reset="earliest",
        )
        await reader.start()
        try:
            produced = await asyncio.wait_for(reader.getone(), _TIMEOUT_SECONDS)
            return bytes(produced.value or b"")
        finally:
            await reader.stop()
    finally:
        await producer.stop()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_profile_parity_in_memory_profile_matches_the_direct_handler() -> None:
    payload = _request_bytes()
    direct = HandlerNetworkGraphCompute().handle(
        ModelNetworkGraphRequest.model_validate_json(payload)
    )
    through_bus = await run_in_memory(payload)
    assert _digest(through_bus) == direct.digest()
    assert [v.key for v in direct.vertices] == [
        "repo_a/bad",
        "repo_a/pub",
        "repo_a/sub",
        "repo_b/pub",
    ]


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_parity_redpanda_profile_digest_equals_in_memory(
    integration_kafka_bootstrap: str,
) -> None:
    payload = _request_bytes()
    in_memory = await run_in_memory(payload)
    redpanda = await run_redpanda(integration_kafka_bootstrap, payload)
    assert _digest(redpanda) == _digest(in_memory)
