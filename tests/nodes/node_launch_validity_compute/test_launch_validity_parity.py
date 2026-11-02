# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The same decisions on the in-memory overlay and on the Redpanda overlay.

Only the overlay's ``transport`` differs: the request goes onto the command topic of the
contract, the bus binding runs the one handler, and the decision is read off the decided
topic. The Redpanda case runs against the declared test lane and is opt-in (kafka and
integration markers), like the other durable-bus proofs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_launch_validity_compute import NodeLaunchValidityCompute
from omnimarket.nodes.node_launch_validity_compute.models import (
    EnumLaunchValidityTransport,
    EnumRead,
    ModelLaunchValidityRequest,
    ModelLaunchValidityResult,
    ModelLaunchValidityTopics,
    ModelStateRead,
)
from omnimarket.nodes.node_launch_validity_compute.overlay import (
    load_launch_validity_policy,
    load_topics,
)
from omnimarket.nodes.node_launch_validity_compute.protocols import (
    ProtocolLaunchValidityBus,
)
from tests.node_overlay_support import install_node_overlay

from .helpers import HEAD, OTHER_HEAD, InMemoryBus, fresh, good_reads, request

SCENARIOS: dict[str, dict[EnumRead, ModelStateRead]] = {
    "valid": good_reads(),
    "queued": good_reads()
    | {EnumRead.PR: fresh(state="OPEN", head=HEAD, pr_class="queued")},
    "merged": good_reads()
    | {EnumRead.PR: fresh(state="MERGED", head=HEAD, pr_class="merged")},
    "moved-head": good_reads() | {EnumRead.PR: fresh(state="OPEN", head=OTHER_HEAD)},
    "ticket-done": good_reads()
    | {EnumRead.TICKET: fresh(done_by=("example-org/example-repo#9",))},
    "owned": good_reads() | {EnumRead.OWNER: fresh(owners=("lane-b",))},
    "read-failed": good_reads()
    | {EnumRead.PR: ModelStateRead(ok=False, detail="unreadable")},
}


def test_launch_validity_contract_topics() -> None:
    """The topics the decision travels on are the contract's: a request in, a decision out, a would-refuse record out."""
    expected = ModelLaunchValidityTopics(
        command="onex.cmd.omnimarket.launch-validity-requested.v1",
        decided="onex.evt.omnimarket.launch-validity-decided.v1",
        would_refuse="onex.evt.omnimarket.launch-validity-would-refuse.v1",
    )
    assert load_topics() == expected


def unique_topics(base: ModelLaunchValidityTopics) -> ModelLaunchValidityTopics:
    """Topics unique to this run, so the topic holds no record from an earlier run."""
    suffix = uuid4().hex[:12]
    return ModelLaunchValidityTopics(
        command=f"{base.command.removesuffix('.v1')}.{suffix}.v1",
        decided=f"{base.decided.removesuffix('.v1')}.{suffix}.v1",
        would_refuse=f"{base.would_refuse.removesuffix('.v1')}.{suffix}.v1",
    )


async def bind_node(
    bus: ProtocolLaunchValidityBus, topics: ModelLaunchValidityTopics
) -> Callable[[], Awaitable[None]]:
    """What the runtime does for a contract: the command topic's records go to the handler, its decision to the decided topic."""
    node = NodeLaunchValidityCompute()

    async def on_command(message: ModelEventMessage) -> None:
        result = node.handle(
            ModelLaunchValidityRequest.model_validate_json(message.value)
        )
        key = result.lane.encode()
        await bus.publish(topics.decided, key, result.model_dump_json().encode())
        if result.would_refuse is not None:
            await bus.publish(
                topics.would_refuse, key, result.would_refuse.model_dump_json().encode()
            )

    return await bus.subscribe_ungrouped(topics.command, on_command)


async def decide_over(
    bus: ProtocolLaunchValidityBus,
    topics: ModelLaunchValidityTopics,
    req: ModelLaunchValidityRequest,
) -> ModelLaunchValidityResult:
    decided: asyncio.Queue[ModelLaunchValidityResult] = asyncio.Queue()

    async def observe(message: ModelEventMessage) -> None:
        await decided.put(ModelLaunchValidityResult.model_validate_json(message.value))

    stop_observing = await bus.subscribe_ungrouped(topics.decided, observe)
    stop_binding = await bind_node(bus, topics)
    try:
        await bus.publish(
            topics.command, req.target.lane.encode(), req.model_dump_json().encode()
        )
        return await asyncio.wait_for(decided.get(), timeout=30)
    finally:
        await stop_binding()
        await stop_observing()


async def assert_parity(
    bus: ProtocolLaunchValidityBus,
    transport: EnumLaunchValidityTransport,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_node_overlay(
        monkeypatch, tmp_path, "launch_validity", {"transport": transport.value}
    )
    assert load_launch_validity_policy().transport is transport, (
        "the overlay selects the bus binding"
    )
    node = NodeLaunchValidityCompute()
    topics = unique_topics(load_topics())
    refused = 0
    for name, reads in SCENARIOS.items():
        req = request(reads, environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
        direct = node.handle(req)
        over_bus = await decide_over(bus, topics, req)
        assert over_bus == direct, name
        assert over_bus.mode.value == "report_only"
        refused += bool(direct.refusals)
    assert refused == len(SCENARIOS) - 1, "falsifier: a scenario that was not refused"


async def test_launch_validity_parity_in_memory_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await assert_parity(
        InMemoryBus(), EnumLaunchValidityTransport.IN_MEMORY, monkeypatch, tmp_path
    )


@pytest.mark.kafka
@pytest.mark.integration
async def test_launch_validity_parity_redpanda_overlay(
    kafka_integration_bus: ProtocolLaunchValidityBus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await assert_parity(
        kafka_integration_bus,
        EnumLaunchValidityTransport.REDPANDA,
        monkeypatch,
        tmp_path,
    )
