# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-resolved guard round trip over the runtime's real in-memory bus."""

from __future__ import annotations

import asyncio
import importlib
from importlib.resources import files
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.model_internal_release_path_guard import (
    ModelInternalReleasePathGuardRequest,
    ModelInternalReleasePathGuardResult,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actions", "allowed"),
    [
        ("Cut a release of omnibase_core to unblock the consumer.", False),
        ("Use the sibling dev tag after the integration tests pass.", True),
    ],
)
async def test_golden_and_refusal_chains(actions: str, allowed: bool) -> None:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_internal_release_path_guard_compute")
        .joinpath("contract.yaml")
        .read_text()
    )
    spec = contract["handler"]
    handler = getattr(importlib.import_module(spec["module"]), spec["class"])()
    request_topic = contract["runtime_dispatch"]["command_topic"]
    terminal_topic = contract["terminal_event"]
    assert (
        request_topic == "onex.cmd.omnimarket.internal-release-path-check-requested.v1"
    )
    assert contract["event_bus"]["subscribe_topics"] == [request_topic]
    assert terminal_topic == "onex.evt.omnimarket.internal-release-path-checked.v1"
    assert contract["event_bus"]["publish_topics"] == [terminal_topic]
    bus = EventBusInmemory()
    received: asyncio.Future[tuple[str, ModelInternalReleasePathGuardResult]] = (
        asyncio.get_running_loop().create_future()
    )

    async def handle(message: Any) -> None:
        assert message.topic == request_topic
        request = ModelInternalReleasePathGuardRequest.model_validate_json(
            message.value
        )
        result = handler.handle(request)
        await bus.publish(
            terminal_topic, key=None, value=result.model_dump_json().encode()
        )

    async def collect(message: Any) -> None:
        received.set_result(
            (
                message.topic,
                ModelInternalReleasePathGuardResult.model_validate_json(message.value),
            )
        )

    await bus.start()
    try:
        unsubscribe_request = await bus.subscribe(
            request_topic, on_message=handle, group_id="guard-request"
        )
        unsubscribe_result = await bus.subscribe(
            terminal_topic, on_message=collect, group_id="guard-result"
        )
        try:
            await bus.publish(
                request_topic,
                key=None,
                value=ModelInternalReleasePathGuardRequest(proposed_actions=actions)
                .model_dump_json()
                .encode(),
            )
            topic, result = await asyncio.wait_for(received, timeout=5)
            assert topic == "onex.evt.omnimarket.internal-release-path-checked.v1"
            assert result.allowed is allowed
            assert result.exit_code == (0 if allowed else 1)
            assert bool(result.violations) is not allowed
            assert bool(result.remediation) is not allowed
        finally:
            await unsubscribe_result()
            await unsubscribe_request()
    finally:
        await bus.close()
