# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A command on node_plan_first_slice_compute's declared topic reaches it through the runtime's own auto-wiring (OMN-25454).

The node's contract used to declare a command topic but no subscribe topic and no runtime profile, so
the runtime never attached it and no demand could reach it. This test loads the real contract from
disk, boots it through ``wire_from_manifest`` (the call the kernel makes) on an in-memory bus,
publishes one command on the contract's command topic and reads the typed answer off the contract's
success terminal event topic. Only the bus is in memory; the contract, handler and dispatch engine
are the runtime's own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_plan_first_slice_compute"
    / "contract.yaml"
)

PLAN_WITH_SLICE = (
    "# Plan\n\n## First slice\n\n" + " ".join(f"word{i}" for i in range(30)) + "\n"
)
CASES = [
    (
        {"plan_text": "# Overview\nThere is no first slice here.\n"},
        {"verdict": "no_first_slice_section"},
    ),
    ({"plan_text": PLAN_WITH_SLICE}, {"verdict": "ok", "heading": "First slice"}),
]
IDS = ["no-first-slice-section", "first-slice-ok"]


def _declared() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT.read_text())


def test_contract_subscribes_its_command_topic_and_runs_on_main() -> None:
    contract = _declared()
    command_topic = contract["runtime_dispatch"]["command_topic"]
    subscribe = contract["event_bus"]["subscribe_topics"]
    assert command_topic in subscribe, (
        "no subscribe topic: the runtime never attaches this node"
    )
    terminal = contract["runtime_dispatch"]["terminal_events"]["success"]
    assert terminal in contract["event_bus"]["publish_topics"]
    assert contract["descriptor"]["runtime_profiles"] == ["main"]


def test_contract_publishes_the_declared_terminal_topics() -> None:
    contract = _declared()
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": "onex.evt.omnimarket.plan-first-slice-check-completed.v1",
        "failure": "onex.evt.omnimarket.plan-first-slice-check-failed.v1",
    }
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.plan-first-slice-check-completed.v1",
    ]


@pytest.mark.parametrize(("payload", "expected"), CASES, ids=IDS)
async def test_command_on_the_declared_topic_is_handled_and_answered(
    payload: dict[str, object], expected: dict[str, str]
) -> None:
    contract = _declared()
    command_topic = contract["runtime_dispatch"]["command_topic"]
    terminal_topic = contract["runtime_dispatch"]["terminal_events"]["success"]
    assert isinstance(command_topic, str)
    assert isinstance(terminal_topic, str)

    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="plan-first-slice", group="plan-first-slice")
    await bus.start()
    answers: list[bytes] = []
    answered = asyncio.Event()

    async def _collect(message: object) -> None:
        answers.append(bytes(getattr(message, "value", b"")))
        answered.set()

    await bus.subscribe(terminal_topic, on_message=_collect, group_id="probe")
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    assert report.total_wired == 1
    engine.freeze()

    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=payload, correlation_id=uuid4()
    )
    await bus.publish(command_topic, None, envelope.model_dump_json().encode(), None)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(answered.wait(), timeout=20)
    await bus.close()

    assert len(answers) == 1, "the node never answered the command"
    body = json.loads(answers[0])["payload"]
    for field, value in expected.items():
        assert body[field] == value, body
