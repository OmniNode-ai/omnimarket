# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay budget changes produce matching direct and bus-delivered findings."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.event_bus.models.model_event_headers import ModelEventHeaders
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_work_progress_compute import NodeWorkProgressCompute
from omnimarket.nodes.node_work_progress_compute.models import (
    ModelWorkProgressRequest,
    ModelWorkProgressResult,
    ModelWorkProgressSubject,
)
from omnimarket.nodes.node_work_progress_compute.overlay import (
    NODE_NAME,
    load_budgets,
    load_phase_notes,
)
from tests.node_overlay_support import install_node_overlay


class ProtocolWorkProgressBus(Protocol):
    """The publish and subscription operations used by this profile proof."""

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object: ...

    async def subscribe_ungrouped(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]: ...


class InMemoryBus:
    """Publishing calls the topic's subscribers in registration order."""

    def __init__(self) -> None:
        self.subscribers: dict[
            str, list[Callable[[ModelEventMessage], Awaitable[None]]]
        ] = {}

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object:
        message = ModelEventMessage(
            topic=topic,
            key=key,
            value=value,
            headers=ModelEventHeaders(
                source="work-progress-test",
                event_type=topic,
                timestamp=datetime.now(UTC),
            ),
        )
        for callback in list(self.subscribers.get(topic, [])):
            await callback(message)
        return None

    async def subscribe_ungrouped(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]:
        self.subscribers.setdefault(topic, []).append(on_message)

        async def unsubscribe() -> None:
            self.subscribers[topic].remove(on_message)

        return unsubscribe


def _unique() -> tuple[str, str]:
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{NODE_NAME}")
        .joinpath("contract.yaml")
        .read_text("utf-8")
    )
    bus = contract["event_bus"]
    command, computed = bus["subscribe_topics"][0], bus["publish_topics"][0]
    assert command == contract["runtime_dispatch"]["command_topic"]
    assert command == contract["handler_routing"]["handlers"][0]["topic"]
    assert computed == contract["terminal_event"]
    assert computed in contract["externally_consumed_topics"]
    suffix = uuid4().hex[:12]
    return (
        f"{command.removesuffix('.v1')}.{suffix}.v1",
        f"{computed.removesuffix('.v1')}.{suffix}.v1",
    )


def test_contract_topics() -> None:
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{NODE_NAME}")
        .joinpath("contract.yaml")
        .read_text("utf-8")
    )
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == "onex.cmd.omnimarket.work-progress-compute-requested.v1"
    )
    assert contract["terminal_event"] == "onex.evt.omnimarket.work-progress-computed.v1"


async def _decide_over(
    bus: ProtocolWorkProgressBus,
    topics: tuple[str, str],
    node: NodeWorkProgressCompute,
    request: ModelWorkProgressRequest,
) -> ModelWorkProgressResult:
    command, computed = topics
    results: asyncio.Queue[ModelWorkProgressResult] = asyncio.Queue()

    async def on_command(message: ModelEventMessage) -> None:
        result = node.handle(
            ModelWorkProgressRequest.model_validate_json(message.value)
        )
        await bus.publish(computed, None, result.model_dump_json().encode())

    async def observe(message: ModelEventMessage) -> None:
        await results.put(ModelWorkProgressResult.model_validate_json(message.value))

    stop_observing = await bus.subscribe_ungrouped(computed, observe)
    stop_binding = await bus.subscribe_ungrouped(command, on_command)
    try:
        await bus.publish(command, None, request.model_dump_json().encode())
        return await asyncio.wait_for(results.get(), timeout=30)
    finally:
        await stop_binding()
        await stop_observing()


async def _assert_profile(
    bus: ProtocolWorkProgressBus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    subjects = (
        ModelWorkProgressSubject(
            lane="rf0925-c2",
            phase="claimed",
            entered_at=now - timedelta(seconds=3840),
            entering_row_id="claimed-entry",
            owner="orchestrator-31d9",
        ),
    )
    node = NodeWorkProgressCompute()
    results = []
    for claimed_budget in (3600, 4000):
        install_node_overlay(
            monkeypatch,
            tmp_path,
            NODE_NAME,
            {
                "budget_seconds": {
                    "placed": 600,
                    "claimed": claimed_budget,
                    "pr_open": 7200,
                }
            },
        )
        request = ModelWorkProgressRequest(
            now=now,
            budgets=load_budgets(),
            phase_notes=load_phase_notes(),
            subjects=subjects,
        )
        over_bus = await _decide_over(bus, _unique(), node, request)
        assert over_bus.findings == node.handle(request).findings
        results.append(over_bus)
    assert [finding.lane for finding in results[0].findings] == ["rf0925-c2"]
    assert results[1].findings == ()
    assert results[0].findings != results[1].findings


async def test_work_progress_in_memory_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _assert_profile(InMemoryBus(), monkeypatch, tmp_path)


@pytest.mark.kafka
@pytest.mark.integration
async def test_work_progress_redpanda_overlay_profile(
    kafka_integration_bus: ProtocolWorkProgressBus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _assert_profile(kafka_integration_bus, monkeypatch, tmp_path)
