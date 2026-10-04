# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""N delegation calls over the runtime port leave no consumer group behind.

The port waits for one reply on the completed and failed topics that every
caller shares. A stable group shared by concurrent callers would split those
topics' partitions among them and hand a reply to the wrong caller, so each call
keeps a group of its own and the group is deleted once the call has left it.
"""

from __future__ import annotations

import asyncio
import json
from typing import cast
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
    ProtocolDelegationEventBus,
    RuntimeDelegationDispatchPort,
    load_runtime_delegation_dispatch_config,
)
from tests.helpers.fake_group_broker import FakeGroupBroker, install_fake_admin

pytestmark = pytest.mark.unit

CALLS = 5


async def _call(port: RuntimeDelegationDispatchPort, correlation_id: UUID) -> object:
    return await port.dispatch(
        prompt="p",
        task_type="test",
        correlation_id=correlation_id,
        max_tokens=None,
        source_file_path=None,
        source_session_id=None,
        wait=True,
        execution_timeout_seconds=30,
        terminal_delivery_margin_seconds=5,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
        tenant_id=None,
    )


def _port(broker: FakeGroupBroker) -> RuntimeDelegationDispatchPort:
    return RuntimeDelegationDispatchPort(
        event_bus=cast(ProtocolDelegationEventBus, broker)
    )


def _answer_every_request(broker: FakeGroupBroker, completed_topic: str) -> None:
    async def on_publish(topic: str, value: bytes) -> None:
        if topic == broker_command_topic:
            correlation_id = json.loads(value)["correlation_id"]
            await broker.deliver(
                completed_topic,
                json.dumps({"payload": {"correlation_id": correlation_id}}).encode(),
            )

    broker_command_topic = load_runtime_delegation_dispatch_config().topics.command
    broker.on_publish = on_publish


def test_group_not_leaked_after_completed_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = FakeGroupBroker()
    install_fake_admin(monkeypatch, broker)
    _answer_every_request(
        broker, load_runtime_delegation_dispatch_config().topics.completed
    )
    port = _port(broker)

    async def scenario() -> None:
        for _ in range(CALLS):
            result = await _call(port, uuid4())
            assert cast("dict[str, object]", result)["status"] == "completed"

    asyncio.run(scenario())

    assert broker.live == set()
    assert broker.empty_groups == set()


def test_group_not_leaked_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    broker = FakeGroupBroker()
    install_fake_admin(monkeypatch, broker)
    config = load_runtime_delegation_dispatch_config().model_copy(
        update={"wait_timeout_seconds": 1}
    )
    port = RuntimeDelegationDispatchPort(
        event_bus=cast(ProtocolDelegationEventBus, broker), config=config
    )

    async def scenario() -> None:
        result = await _call(port, uuid4())
        assert cast("dict[str, object]", result)["status"] == "timeout"

    asyncio.run(scenario())

    assert broker.groups == set()


def test_concurrent_calls_do_not_share_a_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falsifies the stable-group alternative: callers must not share a group."""
    broker = FakeGroupBroker()
    install_fake_admin(monkeypatch, broker)
    port = _port(broker)
    seen: list[set[str]] = []

    async def on_publish(topic: str, value: bytes) -> None:
        subscribed = {group for _, group in broker.active}
        if len(subscribed) >= 2 and not seen:
            seen.append(subscribed)
            for cid in (json.loads(v)["correlation_id"] for _, v in broker.published):
                await broker.deliver(
                    load_runtime_delegation_dispatch_config().topics.completed,
                    json.dumps({"payload": {"correlation_id": cid}}).encode(),
                )

    broker.on_publish = on_publish

    async def scenario() -> None:
        await asyncio.gather(_call(port, uuid4()), _call(port, uuid4()))

    asyncio.run(scenario())

    assert len(seen[0]) == 2
    assert broker.groups == set()
