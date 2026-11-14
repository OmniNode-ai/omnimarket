# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One seam-crossing-name command reaches one completed event through the runtime's wiring.

The golden chain in this directory runs the handler directly. A deployed runtime does not:
it discovers the installed contract, wires its handler into the ``MessageDispatchEngine``
with ``wire_from_manifest`` and subscribes the contract's command topic on the bus. These
cases drive exactly that path on the in-memory bus, with synthetic crossings, and read the
completed topic by correlation id. The command and terminal topics are read from the
contract, never typed here.
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

import omnimarket.nodes.node_seam_crossing_name_compute as node_package
from omnimarket.models.seam_crossing_name import (
    ModelSeamCrossing,
    ModelSeamCrossingNameRequest,
    ModelSeamCrossingNaming,
)
from omnimarket.models.seam_field_diff import (
    ModelSeamFieldFacts,
    ModelSeamModelSummary,
)

pytestmark = pytest.mark.unit

NODE = "node_seam_crossing_name_compute"
NODE_DIR = Path(node_package.__file__).parent
EVENT_TYPE = "omnimarket.seam-crossing-name-requested"
WAIT_SECONDS = 5


def _contract_topics() -> tuple[str, str, str]:
    """The command and the two terminal topics, read from the contract, never typed here."""
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    return (
        contract["runtime_dispatch"]["command_topic"],
        contract["runtime_dispatch"]["terminal_events"]["success"],
        contract["runtime_dispatch"]["terminal_events"]["failure"],
    )


COMMAND_TOPIC, SUCCESS_TOPIC, FAILURE_TOPIC = _contract_topics()


def _consumer_summary() -> ModelSeamModelSummary:
    """A consumer model that declares required field ``a`` and optional field ``b``."""
    return ModelSeamModelSummary(
        model_ref="mod.C",
        fields={
            "a": ModelSeamFieldFacts(
                type_name="any", required=True, nullable=False, has_default=False
            ),
            "b": ModelSeamFieldFacts(
                type_name="any", required=False, nullable=False, has_default=True
            ),
        },
        nested={},
        extra_forbidden=False,
        has_adapter=False,
    )


def _missing_required_payload() -> dict[str, Any]:
    """A crossing whose consumer requires ``a`` but whose body carries only ``b``."""
    request = ModelSeamCrossingNameRequest(
        crossings=(
            ModelSeamCrossing(
                crossing_id="c-missing",
                seam_id="s1",
                topic="t",
                consumer=_consumer_summary(),
                body_keys=("b",),
            ),
        ),
    )
    return request.model_dump(mode="json")


def _undeclared_key_payload() -> dict[str, Any]:
    """A crossing with every required field present plus an undeclared key ``zzz``."""
    request = ModelSeamCrossingNameRequest(
        crossings=(
            ModelSeamCrossing(
                crossing_id="c-undeclared",
                seam_id="s2",
                topic="t",
                consumer=_consumer_summary(),
                body_keys=("a", "zzz"),
            ),
        ),
    )
    return request.model_dump(mode="json")


def _malformed_payload() -> dict[str, Any]:
    """A payload whose ``crossings`` is a string, outside the request model."""
    return {"crossings": "not-a-list"}


class WiredBus:
    """The in-memory bus with the node wired the way the runtime wires it."""

    def __init__(self, bus: EventBusInmemory) -> None:
        self.bus = bus
        self.completed: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def command(
        self, payload: dict[str, Any], *, topic: str = COMMAND_TOPIC
    ) -> UUID:
        correlation_id = uuid4()
        envelope = ModelEventEnvelope[object](
            payload=payload, correlation_id=correlation_id, event_type=EVENT_TYPE
        )
        await self.bus.publish(topic, None, envelope.model_dump_json().encode(), None)
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
    bus = EventBusInmemory(environment="local", group="seam-crossing-name-wiring")
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


def _naming_from_contract() -> ModelSeamCrossingNaming:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    naming = contract["config"]["naming"]
    return ModelSeamCrossingNaming(
        missing_required=naming["missing_required"],
        undeclared_key=naming["undeclared_key"],
        clean=naming["clean"],
    )


@pytest.mark.asyncio
async def test_a_missing_required_crossing_reaches_one_completed_event(
    wired: WiredBus,
) -> None:
    naming = _naming_from_contract()
    correlation_id = await wired.command(_missing_required_payload())
    event = await wired.completed_for(correlation_id)
    verdict = event["payload"]
    name = verdict["names"][0]
    assert name["name"] == naming.missing_required
    assert name["decided_by"] == "rule_r"
    assert name["fields"] == ["a"]
    # One command, one completed event: nothing else carries this correlation id.
    await asyncio.sleep(0.2)
    assert all(
        e["correlation_id"] != str(correlation_id) for e in _drain(wired.completed)
    )


@pytest.mark.asyncio
async def test_an_undeclared_key_crossing_reaches_one_completed_event(
    wired: WiredBus,
) -> None:
    naming = _naming_from_contract()
    correlation_id = await wired.command(_undeclared_key_payload())
    event = await wired.completed_for(correlation_id)
    verdict = event["payload"]
    name = verdict["names"][0]
    assert name["name"] == naming.undeclared_key
    assert name["decided_by"] == "key_check"
    assert name["fields"] == ["zzz"]


@pytest.mark.asyncio
async def test_a_malformed_payload_produces_no_completed_event(wired: WiredBus) -> None:
    correlation_id = await wired.command(_malformed_payload())
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await wired.completed_for(correlation_id)


def _drain(queue: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    drained: list[dict[str, Any]] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    return drained


def test_contract_declares_the_completed_and_failed_terminal_topics() -> None:
    _, completed, failed = _contract_topics()
    assert completed == "onex.evt.omnimarket.seam-crossing-name-completed.v1"
    assert failed == "onex.evt.omnimarket.seam-crossing-name-failed.v1"
