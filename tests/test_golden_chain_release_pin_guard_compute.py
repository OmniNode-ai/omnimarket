# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-resolved release and pin guard round trip over the in-memory bus (OMN-18594)."""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime
from importlib.resources import files
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.model_release_pin_guard import (
    ModelReleasePinGuardRequest,
    ModelReleasePinGuardResult,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "refused"),
    [
        ("gh release create v0.38.70 --repo OmniNode-ai/omnibase_infra", True),
        ("git status", False),
    ],
)
async def test_golden_and_refusal_chains(command: str, refused: bool) -> None:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_release_pin_guard_compute")
        .joinpath("contract.yaml")
        .read_text()
    )
    spec = contract["handler"]
    handler = getattr(importlib.import_module(spec["module"]), spec["class"])()
    request_topic = contract["runtime_dispatch"]["command_topic"]
    terminal_topic = contract["terminal_event"]
    assert request_topic == "onex.cmd.omnimarket.release-pin-check-requested.v1"
    assert contract["event_bus"]["subscribe_topics"] == [request_topic]
    assert terminal_topic == "onex.evt.omnimarket.release-pin-checked.v1"
    assert contract["event_bus"]["publish_topics"] == [terminal_topic]
    bus = EventBusInmemory()
    received: asyncio.Future[tuple[str, ModelReleasePinGuardResult]] = (
        asyncio.get_running_loop().create_future()
    )

    async def handle(message: Any) -> None:
        assert message.topic == request_topic
        request = ModelReleasePinGuardRequest.model_validate_json(message.value)
        result = handler.handle(request)
        await bus.publish(
            terminal_topic, key=None, value=result.model_dump_json().encode()
        )

    async def collect(message: Any) -> None:
        received.set_result(
            (
                message.topic,
                ModelReleasePinGuardResult.model_validate_json(message.value),
            )
        )

    await bus.start()
    try:
        await bus.subscribe(
            request_topic, on_message=handle, group_id="release-pin-guard"
        )
        await bus.subscribe(
            terminal_topic, on_message=collect, group_id="release-pin-guard-test"
        )
        request = ModelReleasePinGuardRequest(
            tool_name="Bash",
            tool_input={"command": command},
            cwd="/work/omnibase_infra",
            now=datetime(2026, 10, 23, 20, 0, 0),
        )
        await bus.publish(
            request_topic, key=None, value=request.model_dump_json().encode()
        )
        topic, result = await asyncio.wait_for(received, timeout=5)
    finally:
        await bus.close()
    assert topic == terminal_topic
    assert result.refuse is refused
    assert result.exit_code == (1 if refused else 0)
