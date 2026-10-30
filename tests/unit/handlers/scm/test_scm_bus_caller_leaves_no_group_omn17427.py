# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A one-shot SCM call reads its terminal with no consumer group (OMN-17427).

The measured failure: the dev broker held 8835 consumer groups, about 6978 of
them one-shot client groups minted per ``onex scm`` call, which unsubscribed and
never left. The caller now reads its terminal through ``subscribe_ungrouped``
and the bus holds no group for it, on either bus. The same body runs against
every bus the parametrisation names; a Redpanda broker is the ``kafka`` leg in
tests/integration/test_one_shot_reply_kafka_groups_omn17427.py.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import BaseModel

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolBusMessage
from omnimarket.delegated_test_loop.lane_bus import (
    LabRunBusError,
    require_one_shot_reply_bus,
)
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.models.scm import ModelScmTopics

FAILURE_TOPIC = "onex.evt.omnimarket.test-one-shot-failed.v1"
TOPICS = ModelScmTopics(
    command="onex.cmd.omnimarket.test-one-shot-requested.v1",
    success="onex.evt.omnimarket.test-one-shot-completed.v1",
    failure=FAILURE_TOPIC,
)
HOST_GROUP = "local.omnimarket.test_one_shot_host.consume.v1"


class _Request(BaseModel):
    verb: str = "pr-view"


class _CountingBus(EventBusInmemory):
    def __init__(self) -> None:
        super().__init__(environment="local", group="one-shot-test")
        self.ungrouped_topics: list[str] = []

    async def subscribe_ungrouped(
        self, topic: str, on_message: Callable[[Any], Awaitable[None]]
    ) -> Callable[[], Awaitable[None]]:
        self.ungrouped_topics.append(topic)
        return await super().subscribe_ungrouped(topic, on_message)


async def _serve(bus: EventBusInmemory) -> Callable[[], Awaitable[None]]:
    async def on_command(message: ProtocolBusMessage) -> None:
        raw = json.loads(message.value)
        reply = ModelEventEnvelope[dict[str, object]](
            payload={"outcome": "completed"},
            correlation_id=raw["correlation_id"],
            parent_envelope_id=raw["envelope_id"],
            event_type="omnimarket.test-one-shot-completed",
        )
        await bus.publish(
            TOPICS.success, None, json.dumps(reply.model_dump(mode="json")).encode()
        )

    return await bus.subscribe(
        TOPICS.command, on_message=on_command, group_id=HOST_GROUP
    )


@pytest.mark.asyncio
async def test_n_calls_leave_the_bus_with_the_groups_it_started_with() -> None:
    bus = _CountingBus()
    await bus.start()
    stop_host = await _serve(bus)
    try:
        groups_before = set(bus.get_consumer_groups().values())
        assert groups_before == {HOST_GROUP}
        for _ in range(8):
            terminal = await ScmBusCaller(bus).call_topics(
                topics=TOPICS,
                request=_Request(),
                correlation_id=uuid.uuid4(),
                timeout_s=2,
            )
            assert terminal.payload == {"outcome": "completed"}
            assert terminal.failed is False
        assert set(bus.get_consumer_groups().values()) == groups_before
        assert not bus._subscribers.get(TOPICS.success)
        assert not bus._subscribers.get(FAILURE_TOPIC)
        assert bus.ungrouped_topics == [TOPICS.success, FAILURE_TOPIC] * 8
    finally:
        await stop_host()
        await bus.close()


@pytest.mark.asyncio
async def test_a_timed_out_call_also_leaves_no_group() -> None:
    bus = _CountingBus()
    await bus.start()
    try:
        with pytest.raises(TimeoutError):
            await ScmBusCaller(bus).call_topics(
                topics=TOPICS,
                request=_Request(),
                correlation_id=uuid.uuid4(),
                timeout_s=0.05,
            )
        assert bus.get_consumer_groups() == {}
    finally:
        await bus.close()


def test_a_bus_without_the_group_less_read_is_refused_at_construction() -> None:
    class GroupOnlyBus:
        async def publish(
            self, topic: str, key: bytes | None, value: bytes, headers: Any = None
        ) -> None:
            return None

        async def subscribe(
            self,
            topic: str,
            node_identity: Any = None,
            on_message: Any = None,
            *,
            group_id: str | None = None,
        ) -> Callable[[], Awaitable[None]]:
            raise AssertionError("a one-shot caller must not subscribe with a group")

    with pytest.raises(LabRunBusError, match="subscribe_ungrouped"):
        require_one_shot_reply_bus(cast("Any", GroupOnlyBus()))
    with pytest.raises(LabRunBusError, match="subscribe_ungrouped"):
        ScmBusCaller(cast("Any", GroupOnlyBus()))
