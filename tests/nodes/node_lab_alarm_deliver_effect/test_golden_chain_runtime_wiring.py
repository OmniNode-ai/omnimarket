# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A command on the delivery's declared topic reaches it through the runtime's own auto-wiring (OMN-25270).

A node whose contract declares no subscribe topic is never attached by a runtime, so no demand reaches it and an
integration pass cannot read it running. This test loads the real contract of node_lab_alarm_deliver_effect from
disk, boots it through ``wire_from_manifest`` (the call the kernel makes) on an in-memory bus with the node's
overlay on the in-memory backend, publishes one probe and one alarm on the contract's command topic, and reads the
typed result of each off the contract's terminal event topic. Only the bus and the delivery backend are in memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
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

NODE_DIR = (
    Path(__file__).parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_lab_alarm_deliver_effect"
)
CONTRACT = NODE_DIR / "contract.yaml"


def _declared() -> dict[str, object]:
    return yaml.safe_load(CONTRACT.read_text())  # type: ignore[no-any-return]


def test_contract_declares_one_command_topic_and_one_terminal_event() -> None:
    contract = _declared()
    subscribe = contract["event_bus"]["subscribe_topics"]  # type: ignore[index]
    assert subscribe, "no subscribe topic: the runtime never attaches this node"
    assert contract["runtime_dispatch"]["command_topic"] == subscribe[0]  # type: ignore[index]
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]  # type: ignore[index]
    assert contract["runtime_profiles"] == ["main"]


async def test_probe_and_alarm_on_the_declared_topic_are_handled_and_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = tmp_path / "roots"
    (roots / "node_lab_alarm_deliver_effect").mkdir(parents=True)
    example = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    example["state_path"] = str(tmp_path / "delivery-state.json")
    (roots / "node_lab_alarm_deliver_effect" / "overlay.yaml").write_text(
        yaml.safe_dump(example)
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(roots))
    contract = _declared()
    command_topic = contract["event_bus"]["subscribe_topics"][0]  # type: ignore[index]
    terminal_topic = contract["terminal_event"]
    assert isinstance(terminal_topic, str)

    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="lab-alarm-deliver", group="lab-alarm-deliver")
    await bus.start()
    answers: list[dict[str, object]] = []
    both = asyncio.Event()

    async def _collect(message: object) -> None:
        body = bytes(getattr(message, "value", b""))
        answers.append(json.loads(body))
        if len(answers) == 2:
            both.set()

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

    alarm = {
        "correlation_id": "run-1",
        "condition": "consumer_group_lag",
        "outcome": "ALARM",
        "alarms": [
            {
                "condition": "consumer_group_lag",
                "subject": "group-a",
                "detail": "lag 900 over bound 100",
            }
        ],
        "write": True,
    }
    probe = {**alarm, "correlation_id": "probe-1", "write": False}
    for payload in (probe, alarm):
        envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
            payload=payload, correlation_id=uuid4()
        )
        await bus.publish(
            command_topic, None, envelope.model_dump_json().encode(), None
        )
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(both.wait(), timeout=20)
    await bus.close()

    assert len(answers) == 2, "the node did not answer both commands"
    results = [a["payload"] for a in answers]  # type: ignore[index]
    outcomes = {str(r.get("correlation_id")): r.get("outcome") for r in results}
    assert outcomes == {"probe-1": "skipped", "run-1": "delivered"}, results
