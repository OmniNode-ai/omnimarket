# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A command on the node's declared topic reaches it through the runtime's own auto-wiring (OMN-25454).

A node whose contract declares no subscribe topic is never attached by the runtime, so nothing on a
runtime can hand it a demand and an integration pass cannot read it running. This test loads the real
contract of node_ci_red_kind_rule_compute from disk, boots it through ``wire_from_manifest`` (the call
the kernel makes) on an in-memory bus, publishes one failed check's name on the contract's command
topic, and reads the typed answer off the contract's terminal event topic. Only the bus is in memory;
the contract, handler, dispatch engine and result applier are the runtime's own.
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
    / "node_ci_red_kind_rule_compute"
    / "contract.yaml"
)


def _declared() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT.read_text())


def test_contract_declares_one_command_topic_and_one_terminal_event() -> None:
    contract = _declared()
    subscribe = contract["event_bus"]["subscribe_topics"]
    assert subscribe, "no subscribe topic: the runtime never attaches this node"
    assert contract["runtime_dispatch"]["command_topic"] == subscribe[0]
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]


async def test_command_on_the_declared_topic_is_handled_and_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = tmp_path / "rules.yaml"
    overlay.write_text(
        "schema_version: ci_red_kind_rules.v1\n"
        "rules:\n"
        "  - kind: pin-unresolvable\n"
        "    check_name_pattern: 'pypi-pin-resolvability'\n"
        "    ignore_case: true\n"
    )
    monkeypatch.setenv("CI_RED_KIND_RULES_OVERLAY", str(overlay))
    contract = _declared()
    subscribe = contract["event_bus"]["subscribe_topics"]
    assert subscribe, "no subscribe topic: the runtime never attaches this node"
    command_topic = subscribe[0]
    terminal_topic = contract["terminal_event"]
    assert isinstance(terminal_topic, str)

    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="ci-red-kind", group="ci-red-kind")
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
        payload={"check_name": "pypi-pin-resolvability / lock", "log_excerpt": ""},
        correlation_id=uuid4(),
    )
    await bus.publish(command_topic, None, envelope.model_dump_json().encode(), None)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(answered.wait(), timeout=20)
    await bus.close()

    assert len(answers) == 1, "the node never answered the command"
    body = answers[0].decode()
    assert "pin-unresolvable" in body, json.loads(body)
