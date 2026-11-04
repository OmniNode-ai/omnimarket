# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Opt-in broker proof with a disposable topic and an exclusive group prefix.

The shared fixture supplies the publisher and lane checks. Its EventBusKafka
reader appends a per-topic suffix to broker groups. To measure exactly one
broker group per host, this test uses one aiokafka consumer via an adapter
implementing the handler's injected subscription protocol.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaConsumer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import GroupIdNotFound, for_code
from aiokafka.protocol.admin import DeleteGroupsRequest
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.model_event_headers import ModelEventHeaders
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_agent_inbox_spool import HandlerAgentInboxSpool
from omnimarket.nodes.node_agent_inbox_spool.models import ModelAgentInboxSpoolRequest
from tests import conftest as shared_fixtures
from tests.bus_lane_guard import assert_broker_in_declared_lane

from .helpers import msg_row, overlay, record

pytestmark = [pytest.mark.kafka, pytest.mark.integration]


@pytest.fixture(autouse=True)
def spool_bootstrap(monkeypatch: pytest.MonkeyPatch) -> str:
    # The declared test lane's broker; a run may pin another broker of that lane.
    bootstrap = os.environ.get("ONEX_TEST_KAFKA_BOOTSTRAP", "") or str(
        shared_fixtures._KAFKA_BOOTSTRAP
    )
    assert_broker_in_declared_lane(bootstrap)
    # Configure the existing fixture before it starts; never use ambient brokers.
    monkeypatch.setattr(shared_fixtures, "_KAFKA_BOOTSTRAP", bootstrap)
    return bootstrap


class _Admin(AIOKafkaAdminClient):
    async def delete_consumer_groups(self, group_ids: list[str]) -> None:
        """Bridge the installed admin's missing convenience method to its wire API."""
        for group_id in group_ids:
            coordinator = await self.find_coordinator(group_id)
            response = await self._send_request(
                DeleteGroupsRequest([group_id]), coordinator
            )
            for _, code in response.results:
                if code and for_code(code) is not GroupIdNotFound:
                    raise for_code(code)(f"Cannot delete test group {group_id}")


class _KafkaGroupBus:
    """Back the handler's subscription protocol with one real consumer per group."""

    def __init__(self, bootstrap: str, physical_topic: str, topic: str) -> None:
        self.bootstrap = bootstrap
        self.physical_topic = physical_topic
        self.topic = topic
        self.consumer: AIOKafkaConsumer | None = None
        self.task: asyncio.Task[None] | None = None

    async def subscribe(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
        *,
        group_id: str,
        auto_offset_reset: str,
    ) -> Callable[[], Awaitable[None]]:
        assert topic == self.topic
        consumer = AIOKafkaConsumer(
            self.physical_topic,
            bootstrap_servers=self.bootstrap,
            group_id=group_id,
            auto_offset_reset=auto_offset_reset,
            enable_auto_commit=True,
        )
        self.consumer = consumer
        try:
            await consumer.start()
        except BaseException:
            await consumer.stop()
            raise

        async def consume() -> None:
            async for record_ in consumer:
                await on_message(
                    ModelEventMessage(
                        topic=topic,
                        key=record_.key,
                        value=record_.value,
                        partition=record_.partition,
                        offset=str(record_.offset),
                        headers=ModelEventHeaders(
                            source="test",
                            event_type="test",
                            timestamp=datetime.now(UTC),
                        ),
                    )
                )

        self.task = asyncio.create_task(consume())
        return self.close

    async def close(self) -> None:
        try:
            if self.task is not None:
                self.task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.task
        finally:
            if self.consumer is not None:
                await self.consumer.stop()
            self.task = None
            self.consumer = None


async def test_agent_inbox_spool_replays_groups_per_call_redpanda(
    spool_bootstrap: str,
    kafka_integration_bus: EventBusKafka,
    tmp_path: Path,
) -> None:
    suffix = uuid4().hex
    topic = f"inbox-spool-msg-{suffix}"
    prefix = f"inbox-spool-{suffix}"
    config = overlay(tmp_path, consumer_group_prefix=prefix, read_window_seconds=30)
    physical_topic = kafka_integration_bus.resolve_bus_binding(topic).physical_topic
    bus = _KafkaGroupBus(spool_bootstrap, physical_topic, topic)
    admin = _Admin(bootstrap_servers=spool_bootstrap)
    await admin.start()

    async def own_groups() -> set[str]:
        return {
            str(group[0])
            for group in await admin.list_consumer_groups()
            if str(group[0]).startswith(prefix + "-")
        }

    try:
        response = await admin.create_topics(
            [NewTopic(physical_topic, num_partitions=1, replication_factor=1)]
        )
        assert all(code == 0 for _, code, *_ in response.topic_errors)
        assert not await own_groups()
        nonces = ("redpanda-direct", "redpanda-all")
        for to, nonce in (("lane-a", nonces[0]), ("all", nonces[1])):
            await kafka_integration_bus.publish(topic, None, record(msg_row(to, nonce)))
        handler = HandlerAgentInboxSpool(
            bus=bus, clock=lambda: datetime.now(UTC), topics=(topic,)
        )
        seen = 0
        for _ in range(5):
            result = await handler.handle(
                ModelAgentInboxSpoolRequest(
                    host="host-a", lanes=("lane-a",), overlay=config
                )
            )
            assert result.read_ok, result.error
            assert result.consumer_group == f"{prefix}-host-a"
            assert result.groups_used == (f"{prefix}-host-a",)
            seen += result.events_read
        assert seen >= 2
        assert await own_groups() == {f"{prefix}-host-a"}
        found = sorted(p.name for p in (tmp_path / "lane-a" / "msgs").glob("*.json"))
        assert len(found) == 2
        texts = [p.read_text() for p in (tmp_path / "lane-a" / "msgs").glob("*.json")]
        assert all(any(n in t for t in texts) for n in nonces)
        assert (tmp_path / "lane-a" / "unread").exists()
    finally:
        try:
            await bus.close()
        finally:
            try:
                # A unique prefix owns every deleted group. No unrelated group is touched.
                groups = await own_groups()
                if groups:
                    await admin.delete_consumer_groups(sorted(groups))
            finally:
                try:
                    await admin.delete_topics([physical_topic])
                finally:
                    await admin.close()
