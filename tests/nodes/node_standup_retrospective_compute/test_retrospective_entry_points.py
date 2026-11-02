# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21286: the entry points a stand-up run reaches the node by, run on a Friday fixture.

The node is reached by its run envelope: ``onex node node_standup_retrospective_compute`` with the
request as input (what a thin skill calls), or a command record on the contract's command topic,
answered on the built topic. Both are run here on a Friday fixture and on a Thursday one, and the
same section comes back each way. The two-profile case runs the command topic over the in-memory
bus and over the Redpanda bus, selected by the overlay's ``transport``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_infra.event_bus.models.model_event_headers import ModelEventHeaders
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_standup_retrospective_compute import (
    NodeStandupRetrospectiveCompute,
)
from omnimarket.nodes.node_standup_retrospective_compute.models.model_standup_retrospective import (
    EnumStandupRetrospectiveTransport,
    ModelStandupRetrospectiveRequest,
    ModelStandupRetrospectiveResult,
    ModelStandupRetrospectiveTopics,
)
from omnimarket.nodes.node_standup_retrospective_compute.overlay import (
    load_standup_retrospective_policy,
    load_topics,
)
from omnimarket.nodes.node_standup_retrospective_compute.protocols import (
    ProtocolStandupRetrospectiveBus,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

FRIDAY = "2026-10-02T17:50:00Z"
THURSDAY = "2026-10-01T17:50:00Z"
RETRO = [
    {
        "role": "platform engineer",
        "name": "Pat Example",
        "product_use": "delegated a test fix",
        "idea": "show the delegation receipt in the footer",
        "process_change": "pair on the first delegated ticket",
    },
    {
        "role": "operator",
        "product_use": "ran the merge drain",
        "friction": "the drain hides which check cleared",
        "process_change": "name the cleared check",
    },
]


class InMemoryBus:
    """The in-memory overlay's bus: a topic is a list of subscribers, publishing calls them in order."""

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
                source="standup-retrospective-test",
                event_type=topic,
                timestamp=datetime.now(UTC),
            ),
        )
        for callback in list(self.subscribers.get(topic, [])):
            await callback(message)
        return None

    async def subscribe_ungrouped(
        self, topic: str, on_message: Callable[[ModelEventMessage], Awaitable[None]]
    ) -> Callable[[], Awaitable[None]]:
        self.subscribers.setdefault(topic, []).append(on_message)

        async def unsubscribe() -> None:
            self.subscribers[topic].remove(on_message)

        return unsubscribe


def test_contract_topics() -> None:
    assert load_topics() == ModelStandupRetrospectiveTopics(
        command="onex.cmd.omnimarket.standup-retrospective-requested.v1",
        built="onex.evt.omnimarket.standup-retrospective-built.v1",
    )


@pytest.mark.parametrize(
    ("until", "drafts"), [(FRIDAY, 2), (THURSDAY, 0)], ids=["friday", "thursday"]
)
def test_node_run_envelope_on_a_friday_fixture(
    tmp_path: Path, until: str, drafts: int
) -> None:
    """The run envelope (``onex node``, the contract's handler built by the runtime) returns the section on a Friday and none on a Thursday."""
    payload = tmp_path / "input.json"
    payload.write_text(json.dumps({"until": until, "entries": json.dumps(RETRO)}))
    runtime = Path(sys.executable).parent / "onex"
    done = subprocess.run(
        [
            str(runtime),
            "node",
            "node_standup_retrospective_compute",
            "--input",
            str(payload),
            "--state-root",
            str(tmp_path / "state"),
            "--backend",
            "event_bus=inmemory",
            "--timeout",
            "30",
            "--output",
            "receipt",
            "--omnibase-path",
            str(Path(__file__).resolve().parents[3]),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    receipt = json.loads(done.stdout[done.stdout.index('{"skill_name"') :])
    assert receipt["status"] == "success", done.stderr
    result = receipt["result"]
    assert len(result["ticket_drafts"]) == drafts
    assert result["section_lines"][:1] == (["retrospective (2):"] if drafts else [])
    assert [d["credit"] for d in result["ticket_drafts"]] == (
        ["platform engineer", "operator"] if drafts else []
    )
    assert bool(result["retrospective"]) is (drafts > 0)
    assert "Pat Example" not in json.dumps(result)


def unique_topics(
    base: ModelStandupRetrospectiveTopics,
) -> ModelStandupRetrospectiveTopics:
    suffix = uuid4().hex[:12]
    return ModelStandupRetrospectiveTopics(
        command=f"{base.command.removesuffix('.v1')}.{suffix}.v1",
        built=f"{base.built.removesuffix('.v1')}.{suffix}.v1",
    )


async def bind_node(
    bus: ProtocolStandupRetrospectiveBus, topics: ModelStandupRetrospectiveTopics
) -> Callable[[], Awaitable[None]]:
    """What the runtime does for the contract: the command topic's records go to the handler, its result to the built topic."""
    node = NodeStandupRetrospectiveCompute()

    async def on_command(message: ModelEventMessage) -> None:
        result = node.handle(
            ModelStandupRetrospectiveRequest.model_validate_json(message.value)
        )
        await bus.publish(topics.built, None, result.model_dump_json().encode())

    return await bus.subscribe_ungrouped(topics.command, on_command)


async def build_over(
    bus: ProtocolStandupRetrospectiveBus,
    topics: ModelStandupRetrospectiveTopics,
    req: ModelStandupRetrospectiveRequest,
) -> ModelStandupRetrospectiveResult:
    built: asyncio.Queue[ModelStandupRetrospectiveResult] = asyncio.Queue()

    async def observe(message: ModelEventMessage) -> None:
        await built.put(
            ModelStandupRetrospectiveResult.model_validate_json(message.value)
        )

    stop_observing = await bus.subscribe_ungrouped(topics.built, observe)
    stop_binding = await bind_node(bus, topics)
    try:
        await bus.publish(topics.command, None, req.model_dump_json().encode())
        return await asyncio.wait_for(built.get(), timeout=30)
    finally:
        await stop_binding()
        await stop_observing()


async def assert_parity(
    bus: ProtocolStandupRetrospectiveBus,
    transport: EnumStandupRetrospectiveTransport,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_node_overlay(
        monkeypatch, tmp_path, "standup_retrospective", {"transport": transport.value}
    )
    assert load_standup_retrospective_policy().transport is transport, (
        "the overlay selects the bus binding"
    )
    node = NodeStandupRetrospectiveCompute()
    topics = unique_topics(load_topics())
    friday = ModelStandupRetrospectiveRequest(until=FRIDAY, entries=json.dumps(RETRO))
    thursday = ModelStandupRetrospectiveRequest(
        until=THURSDAY, entries=json.dumps(RETRO)
    )
    direct = node.handle(friday)
    assert await build_over(bus, topics, friday) == direct
    assert len(direct.ticket_drafts) == 2, (
        "falsifier: a Friday run that drafted nothing"
    )
    assert await build_over(bus, topics, thursday) == node.handle(thursday)
    assert node.handle(thursday).retrospective == {}


async def test_standup_retrospective_parity_in_memory_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await assert_parity(
        InMemoryBus(),
        EnumStandupRetrospectiveTransport.IN_MEMORY,
        monkeypatch,
        tmp_path,
    )


@pytest.mark.kafka
@pytest.mark.integration
async def test_standup_retrospective_parity_redpanda_overlay(
    kafka_integration_bus: ProtocolStandupRetrospectiveBus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await assert_parity(
        kafka_integration_bus,
        EnumStandupRetrospectiveTransport.REDPANDA,
        monkeypatch,
        tmp_path,
    )
