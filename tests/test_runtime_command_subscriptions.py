# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Checks for typed runtime commands and their contract-declared subscriptions."""

from __future__ import annotations

import asyncio
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

NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def test_typed_runtime_commands_are_subscribed() -> None:
    """A typed command route must be reachable through its declared command topic."""
    checked = 0
    missing = []
    for path in sorted(NODES.glob("node_*/contract.yaml")):
        contract = yaml.load(path.read_text(encoding="utf-8"), Loader=YAML_LOADER)
        dispatch = contract.get("runtime_dispatch") or {}
        routing = contract.get("handler_routing") or {}
        command_topic = dispatch.get("command_topic")
        if routing.get("routing_strategy") != "payload_type_match" or not command_topic:
            continue
        checked += 1
        subscriptions = (contract.get("event_bus") or {}).get("subscribe_topics") or []
        if command_topic not in subscriptions:
            missing.append(f"{path.parent.name}: {command_topic}")
    assert checked, "no typed runtime commands were checked"
    assert not missing, "command topic missing from subscribe_topics:\n" + "\n".join(
        missing
    )


@pytest.mark.parametrize(
    ("name", "payload", "result_field", "expected"),
    [
        (
            "node_plan_first_slice_compute",
            {"plan_text": "# Overview\nThere is no first slice.\n"},
            "verdict",
            "no_first_slice_section",
        ),
        (
            "node_plan_review_ladder_compute",
            {"first_slice_verdict": "no_first_slice_section"},
            "move",
            "return_to_author",
        ),
    ],
)
async def test_plan_command_reaches_its_typed_handler(
    name: str, payload: dict[str, str], result_field: str, expected: str
) -> None:
    path = NODES / name / "contract.yaml"
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    command = contract["runtime_dispatch"]["command_topic"]
    bus_config = contract.get("event_bus") or {}
    assert command in (bus_config.get("subscribe_topics") or []), (
        f"{name} has no subscription for {command}"
    )
    assert contract["descriptor"]["runtime_profiles"] == ["main"]
    terminal = contract["runtime_dispatch"]["terminal_events"]["success"]
    assert terminal in bus_config["publish_topics"]

    discovery = discover_contracts_from_paths([path])
    assert not discovery.errors, discovery.errors
    engine = MessageDispatchEngine()
    bus = EventBusInmemory(environment="plan-review", group="plan-review")
    replies: list[bytes] = []
    answered = asyncio.Event()

    async def collect(message: object) -> None:
        replies.append(bytes(getattr(message, "value", b"")))
        answered.set()

    await bus.start()
    try:
        await bus.subscribe(terminal, on_message=collect, group_id="probe")
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(discovery.contracts)),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert report.total_failed == 0
        assert report.total_wired == 1
        engine.freeze()

        envelope = ModelEventEnvelope[object](payload=payload, correlation_id=uuid4())
        await bus.publish(command, None, envelope.model_dump_json().encode(), None)
        await asyncio.wait_for(answered.wait(), timeout=10)
    finally:
        await bus.close()

    assert len(replies) == 1
    assert json.loads(replies[0])["payload"][result_field] == expected
