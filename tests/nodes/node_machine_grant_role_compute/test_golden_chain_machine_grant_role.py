# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory profile: one resolve command reaches one resolved event through the runtime wiring.

The deployed runtime discovers the installed contract, wires its handler with
``wire_from_manifest`` and subscribes the contract's command topic. These cases drive that path on
the in-memory bus with a fictional overlay injected on the command.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

import omnimarket.nodes.node_machine_grant_role_compute as node_package
from tests.nodes.node_machine_grant_role_compute.overlay_fixture import (
    TWO_WRITERS,
    machine,
    overlay,
)

pytestmark = pytest.mark.unit

NODE = "node_machine_grant_role_compute"
NODE_DIR = Path(node_package.__file__).parent
EVENT_TYPE = "omnimarket.machine-grant-role-resolve-requested"
WAIT_SECONDS = 5


def _contract_topics() -> tuple[str, str]:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    return contract["runtime_dispatch"]["command_topic"], contract["terminal_event"]


COMMAND_TOPIC, RESOLVED_TOPIC = _contract_topics()


class WiredBus:
    def __init__(self, bus: EventBusInmemory) -> None:
        self.bus = bus
        self.resolved: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def command(
        self, payload: dict[str, Any], *, topic: str = COMMAND_TOPIC
    ) -> UUID:
        correlation_id = uuid4()
        envelope = ModelEventEnvelope[object](
            payload=payload, correlation_id=correlation_id, event_type=EVENT_TYPE
        )
        await self.bus.publish(topic, None, envelope.model_dump_json().encode(), None)
        return correlation_id

    async def resolved_for(self, correlation_id: UUID) -> dict[str, Any]:
        async with asyncio.timeout(WAIT_SECONDS):
            while True:
                event = await self.resolved.get()
                if event["correlation_id"] == str(correlation_id):
                    return event


@pytest_asyncio.fixture
async def wired() -> AsyncIterator[WiredBus]:
    contracts = tuple(c for c in discover_contracts().contracts if c.name == NODE)
    assert len(contracts) == 1, f"{NODE} is not discovered from the installed package"
    bus = EventBusInmemory(environment="local", group="machine-grant-role-wiring")
    await bus.start()
    harness = WiredBus(bus)

    async def collect(message: ModelEventMessage) -> None:
        await harness.resolved.put(json.loads(message.value))

    await bus.subscribe(
        RESOLVED_TOPIC, group_id="resolved-collector", on_message=collect
    )
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=contracts),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert {r.contract_name: r.outcome.value for r in report.results} == {NODE: "wired"}
    assert report.results[0].topics_subscribed == (COMMAND_TOPIC,)
    engine.freeze()
    try:
        yield harness
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_a_resolve_command_reaches_the_overlay_bindings(wired: WiredBus) -> None:
    correlation_id = await wired.command({"overlay": TWO_WRITERS, "role": "scm-writer"})
    result = (await wired.resolved_for(correlation_id))["payload"]
    assert result["status"] == "resolved"
    assert [b["principal"] for b in result["bindings"]] == ["lab-host-a", "lab-host-b"]


@pytest.mark.asyncio
async def test_an_undeclared_role_is_answered_refused(wired: WiredBus) -> None:
    correlation_id = await wired.command(
        {"overlay": overlay(machine("lab-host-a", "unknown-role"))}
    )
    result = (await wired.resolved_for(correlation_id))["payload"]
    assert result["status"] == "refused"
    assert result["refusal"] == "grant-role-undeclared"


@pytest.mark.asyncio
async def test_a_command_on_another_topic_gets_no_resolved_event(
    wired: WiredBus,
) -> None:
    """Negative control: the harness reports silence when the node is not addressed."""
    correlation_id = await wired.command(
        {"overlay": TWO_WRITERS}, topic=COMMAND_TOPIC + ".elsewhere"
    )
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await wired.resolved_for(correlation_id)
