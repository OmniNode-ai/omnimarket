# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Opt-in broker proof with disposable topics and an exclusive group prefix.

The shared fixture supplies the publisher and lane checks. Its EventBusKafka
reader appends a per-topic suffix to broker groups. To measure exactly one
broker group per host, this test uses one aiokafka consumer for both topics via
an adapter implementing the handler's injected subscription protocol.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaConsumer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import GroupIdNotFound, for_code
from aiokafka.protocol.admin import DeleteGroupsRequest
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.model_event_headers import ModelEventHeaders
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_host_drift_fleet import HandlerHostDriftFleet
from omnimarket.nodes.node_host_drift_fleet.models import ModelHostDriftFleetRequest
from tests import conftest as shared_fixtures
from tests.bus_lane_guard import assert_broker_in_declared_lane

from .helpers import MemoryStore, heartbeat, overlay

pytestmark = [pytest.mark.kafka, pytest.mark.integration]


@pytest.fixture(autouse=True)
def fleet_bootstrap(monkeypatch: pytest.MonkeyPatch) -> str:
    bootstrap = os.environ.get("ONEX_TEST_KAFKA_BOOTSTRAP", "")
    if not bootstrap:
        pytest.skip("ONEX_TEST_KAFKA_BOOTSTRAP not set; broker integration is opt-in")
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
    """Multiplex the two subscriptions onto a single real consumer per group."""

    def __init__(
        self, bootstrap: str, topics: tuple[str, ...], physical_topics: tuple[str, ...]
    ) -> None:
        self.bootstrap = bootstrap
        self.topics = topics
        self.physical_topics = physical_topics
        self.canonical_topics = dict(zip(physical_topics, topics, strict=True))
        self.callbacks: dict[str, Callable[[ModelEventMessage], Awaitable[None]]] = {}
        self.consumer: AIOKafkaConsumer | None = None
        self.task: asyncio.Task[None] | None = None
        self.group_id: str | None = None

    async def subscribe(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
        *,
        group_id: str,
        auto_offset_reset: str,
    ) -> Callable[[], Awaitable[None]]:
        assert topic in self.topics
        assert self.group_id in (None, group_id)
        self.group_id = group_id
        self.callbacks[topic] = on_message
        if len(self.callbacks) == len(self.topics):
            consumer = AIOKafkaConsumer(
                *self.physical_topics,
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
                self.callbacks.pop(topic)
                raise

            async def consume() -> None:
                async for record in consumer:
                    canonical_topic = self.canonical_topics[record.topic]
                    callback = self.callbacks.get(canonical_topic)
                    if callback is not None:
                        await callback(
                            ModelEventMessage(
                                topic=canonical_topic,
                                key=record.key,
                                value=record.value,
                                partition=record.partition,
                                offset=str(record.offset),
                                headers=ModelEventHeaders(
                                    source="test",
                                    event_type="test",
                                    timestamp=datetime.now(UTC),
                                ),
                            )
                        )

            self.task = asyncio.create_task(consume())

        async def stop() -> None:
            self.callbacks.pop(topic, None)
            if not self.callbacks:
                await self.close()

        return stop

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
            self.group_id = None
            self.callbacks.clear()


async def test_host_drift_fleet_replays_groups_per_call_redpanda(
    fleet_bootstrap: str,
    kafka_integration_bus: EventBusKafka,
) -> None:
    suffix = uuid4().hex
    topics = tuple(
        f"fleet-replay-{kind}-{suffix}" for kind in ("heartbeat", "resolved")
    )
    prefix = f"fleet-replay.{suffix}"
    config = overlay(consumer_group_prefix=prefix, read_window_seconds=30)
    physical_topics = tuple(
        kafka_integration_bus.resolve_bus_binding(topic).physical_topic
        for topic in topics
    )
    bus = _KafkaGroupBus(fleet_bootstrap, topics, physical_topics)
    admin = _Admin(bootstrap_servers=fleet_bootstrap)
    await admin.start()

    async def own_groups() -> set[str]:
        return {
            str(group[0])
            for group in await admin.list_consumer_groups()
            if str(group[0]).startswith(prefix + ".")
        }

    try:
        response = await admin.create_topics(
            [
                NewTopic(topic, num_partitions=1, replication_factor=1)
                for topic in physical_topics
            ]
        )
        assert all(code == 0 for _, code, *_ in response.topic_errors)
        before = await own_groups()
        assert not before
        for topic in topics:
            # Heartbeats on the resolved topic intentionally exercise malformed accounting too.
            for host in config.hosts:
                value = json.dumps(
                    {"payload": heartbeat(host).model_dump(mode="json")}
                ).encode()
                await kafka_integration_bus.publish(topic, None, value)
        for host in config.hosts:
            handler = HandlerHostDriftFleet(
                bus=bus,
                clock=lambda: datetime.now(UTC),
                state_store=MemoryStore(),
                topics=topics,
            )
            seen = 0
            for _ in range(20):
                result = await handler.handle(
                    ModelHostDriftFleetRequest(
                        host=host,
                        previous=None,
                        now=datetime.now(UTC),
                        overlay=config,
                        replay=True,
                    )
                )
                assert result.read_ok, result.error
                assert result.consumer_group == f"{prefix}.{host}"
                assert result.groups_used == (f"{prefix}.{host}",)
                seen += result.events_read
            assert seen >= 4
        after = await own_groups()
        assert after == {f"{prefix}.{host}" for host in config.hosts}
        for host in config.hosts:
            assert (
                len({group for group in after - before if group == f"{prefix}.{host}"})
                <= 1
            )
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
                    await admin.delete_topics(list(physical_topics))
                finally:
                    await admin.close()
