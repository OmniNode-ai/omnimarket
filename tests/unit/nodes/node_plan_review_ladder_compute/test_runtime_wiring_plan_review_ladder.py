# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One plan review ladder command reaches one completed event through the runtime's wiring.

The golden chain next door runs the handler directly. A deployed runtime does not: it
discovers the installed contract, wires its handler into the ``MessageDispatchEngine`` with
``wire_from_manifest`` and subscribes the contract's command topic on the bus. These cases
drive that path on the in-memory bus and read the completed topic by correlation id. The
command and terminal topics are read from the contract, never typed here.
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
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

import omnimarket.nodes.node_plan_review_ladder_compute as node_package

pytestmark = pytest.mark.unit

NODE = "node_plan_review_ladder_compute"
NODE_DIR = Path(node_package.__file__).parent
EVENT_TYPE = "omnimarket.plan-review-ladder-decide-requested"
WAIT_SECONDS = 5
_CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
COMMAND_TOPIC = _CONTRACT["runtime_dispatch"]["command_topic"]
SUCCESS_TOPIC = _CONTRACT["runtime_dispatch"]["terminal_events"]["success"]


def _first_round_payload() -> dict[str, Any]:
    """A plan whose first slice is ok and that has no review round yet."""
    return {
        "first_slice_verdict": "ok",
        "rounds": [],
        "later_rungs": [],
        "settings": {"blocking_kinds": ["wrong_fact"]},
    }


def _drain(queue: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    drained: list[dict[str, Any]] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    return drained


class WiredBus:
    """The in-memory bus with the node wired the way the runtime wires it."""

    def __init__(self, bus: EventBusInmemory) -> None:
        self.bus = bus
        self.completed: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def command(self, payload: dict[str, Any]) -> UUID:
        correlation_id = uuid4()
        envelope = ModelEventEnvelope[object](
            payload=payload, correlation_id=correlation_id, event_type=EVENT_TYPE
        )
        await self.bus.publish(
            COMMAND_TOPIC, None, envelope.model_dump_json().encode(), None
        )
        return correlation_id

    async def completed_for(self, correlation_id: UUID) -> dict[str, Any]:
        """The completed event that carries this correlation id; others are not ours."""
        async with asyncio.timeout(WAIT_SECONDS):
            while True:
                event = await self.completed.get()
                if event["correlation_id"] == str(correlation_id):
                    return event


@pytest_asyncio.fixture
async def wired() -> AsyncIterator[WiredBus]:
    manifest = discover_contracts_from_paths([NODE_DIR / "contract.yaml"])
    assert manifest.total_errors == 0, manifest.errors
    contracts = tuple(c for c in manifest.contracts if c.name == NODE)
    # Positive control: an empty discovery would make every case below vacuous.
    assert len(contracts) == 1, f"{NODE} is not discovered from its contract"
    bus = EventBusInmemory(environment="local", group="plan-review-ladder-wiring")
    await bus.start()
    harness = WiredBus(bus)

    async def collect(message: ModelEventMessage) -> None:
        await harness.completed.put(json.loads(message.value))

    await bus.subscribe(
        SUCCESS_TOPIC, group_id="completed-collector", on_message=collect
    )
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=contracts),
        engine,
        event_bus=bus,
        environment="local",
    )
    outcomes = {r.contract_name: r.outcome.value for r in report.results}
    assert outcomes == {NODE: "wired"}
    assert report.results[0].topics_subscribed == (COMMAND_TOPIC,)
    engine.freeze()
    try:
        yield harness
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_a_plan_with_no_round_yet_reaches_one_completed_event(
    wired: WiredBus,
) -> None:
    correlation_id = await wired.command(_first_round_payload())
    event = await wired.completed_for(correlation_id)
    decision = event["payload"]
    assert decision["move"] == "full_plan_round"
    assert decision["rule"] == "no_round_yet"
    # One command, one completed event: nothing else carries this correlation id.
    await asyncio.sleep(0.2)
    assert all(
        e["correlation_id"] != str(correlation_id) for e in _drain(wired.completed)
    )
