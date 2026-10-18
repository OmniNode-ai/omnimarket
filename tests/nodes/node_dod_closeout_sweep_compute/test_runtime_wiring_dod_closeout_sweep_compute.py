# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21048: one decide-requested command reaches one decided event through the runtime's wiring.

The golden chain in this directory runs the contract on ``RuntimeLocal``. A deployed
runtime does not: it discovers the installed contract, wires its handler into the
``MessageDispatchEngine`` with ``wire_from_manifest`` and subscribes the contract's command
topic on the bus. These cases drive exactly that path on the in-memory bus, with synthetic
rows and a fictional date, and read the decided topic by correlation id.
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

import omnimarket.nodes.node_dod_closeout_sweep_compute as node_package

pytestmark = pytest.mark.unit

NODE = "node_dod_closeout_sweep_compute"
NODE_DIR = Path(node_package.__file__).parent
FICTIONAL_DATE = "2099-01-02"
FICTIONAL_CLOCK = "2099-01-02T10:00:00Z"
EVENT_TYPE = "omnimarket.dod-closeout-sweep-decide-requested"
WAIT_SECONDS = 5


def _contract_topics() -> tuple[str, str]:
    """The command and decided topics, read from the contract, never typed here."""
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    return (
        contract["runtime_dispatch"]["command_topic"],
        contract["terminal_event"],
    )


COMMAND_TOPIC, DECIDED_TOPIC = _contract_topics()


class WiredBus:
    """The in-memory bus with the node wired the way the runtime wires it."""

    def __init__(self, bus: EventBusInmemory) -> None:
        self.bus = bus
        self.decided: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def command(
        self, payload: dict[str, Any], *, topic: str = COMMAND_TOPIC
    ) -> UUID:
        correlation_id = uuid4()
        envelope = ModelEventEnvelope[object](
            payload=payload, correlation_id=correlation_id, event_type=EVENT_TYPE
        )
        await self.bus.publish(topic, None, envelope.model_dump_json().encode(), None)
        return correlation_id

    async def decided_for(self, correlation_id: UUID) -> dict[str, Any]:
        """The decided event that carries this correlation id; others are not ours."""
        async with asyncio.timeout(WAIT_SECONDS):
            while True:
                event = await self.decided.get()
                if event["correlation_id"] == str(correlation_id):
                    return event


@pytest_asyncio.fixture
async def wired() -> AsyncIterator[WiredBus]:
    contracts = tuple(c for c in discover_contracts().contracts if c.name == NODE)
    # Positive control: an empty discovery would make every case below vacuous.
    assert len(contracts) == 1, f"{NODE} is not discovered from the installed package"
    bus = EventBusInmemory(environment="local", group="dod-closeout-wiring")
    await bus.start()
    harness = WiredBus(bus)

    async def collect(message: ModelEventMessage) -> None:
        await harness.decided.put(json.loads(message.value))

    await bus.subscribe(DECIDED_TOPIC, group_id="decided-collector", on_message=collect)
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
async def test_a_precheck_command_reaches_one_decided_event(wired: WiredBus) -> None:
    correlation_id = await wired.command(
        {
            "kind": "precheck",
            "date": FICTIONAL_DATE,
            "clock_utc": FICTIONAL_CLOCK,
            "ledger_rows": [],
        }
    )
    event = await wired.decided_for(correlation_id)
    decision = event["payload"]
    assert decision["kind"] == "precheck"
    assert decision["verdict"] == "run"
    assert decision["checks_failed"] == ["report-missing", "ledger-terminal-missing"]
    # One command, one decided event: nothing else carries this correlation id.
    await asyncio.sleep(0.2)
    assert all(
        e["correlation_id"] != str(correlation_id) for e in _drain(wired.decided)
    )


@pytest.mark.asyncio
async def test_a_plan_scope_command_reaches_a_decided_chunk_plan(
    wired: WiredBus,
) -> None:
    correlation_id = await wired.command(
        {
            "kind": "plan_scope",
            "date": FICTIONAL_DATE,
            "project_id": "11111111-2222-3333-4444-555555555555",
            "tickets": [],
        }
    )
    decision = (await wired.decided_for(correlation_id))["payload"]
    assert decision["kind"] == "plan_scope"
    assert decision["chunks"] == []


@pytest.mark.asyncio
async def test_an_unknown_kind_is_answered_with_a_failed_decided_event(
    wired: WiredBus,
) -> None:
    correlation_id = await wired.command({"kind": "bogus", "date": FICTIONAL_DATE})
    answer = (await wired.decided_for(correlation_id))["payload"]
    assert answer["status"] == "failed"
    assert answer["failure_class"] == "ValidationError"
    assert answer["retryable"] is False
    assert "kind" in answer["failure_reason"]
    assert "bogus" in answer["failure_reason"]
    assert answer["origin_topic"] == COMMAND_TOPIC


@pytest.mark.asyncio
async def test_a_field_outside_the_request_model_is_refused_not_dropped(
    wired: WiredBus,
) -> None:
    correlation_id = await wired.command(
        {
            "kind": "precheck",
            "date": FICTIONAL_DATE,
            "clock_utc": FICTIONAL_CLOCK,
            "ledger_rows": [],
            "requested_by": "someone",
        }
    )
    answer = (await wired.decided_for(correlation_id))["payload"]
    assert answer["status"] == "failed"
    assert "requested_by" in answer["failure_reason"]


@pytest.mark.asyncio
async def test_a_command_on_another_topic_gets_no_decided_event(
    wired: WiredBus,
) -> None:
    """Negative control: the harness reports silence when the node is not addressed."""
    other = COMMAND_TOPIC.replace("decide-requested", "decide-requested-elsewhere")
    correlation_id = await wired.command(
        {
            "kind": "precheck",
            "date": FICTIONAL_DATE,
            "clock_utc": FICTIONAL_CLOCK,
            "ledger_rows": [],
        },
        topic=other,
    )
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await wired.decided_for(correlation_id)


def _drain(queue: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    drained: list[dict[str, Any]] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    return drained
