# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus publisher of the outbox flush: it returns only after the broker's acknowledgement and raises otherwise."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.tenant_event_kafka_publisher import (
    KafkaTenantEventPublisher,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class FakeProducer:
    def __init__(self, fail: bool = False) -> None:
        self.started = 0
        self.stopped = 0
        self.sent: list[tuple[str, bytes, bytes]] = []
        self.fail = fail

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def send_and_wait(self, topic: str, *, value: bytes, key: bytes) -> None:
        if self.fail:
            raise TimeoutError("no ack")
        self.sent.append((topic, key, value))


def _factory(producer: FakeProducer) -> Any:
    async def build() -> FakeProducer:
        return producer

    return build


async def test_publish_waits_for_the_ack_and_sends_key_and_json() -> None:
    producer = FakeProducer()
    publisher = KafkaTenantEventPublisher(_factory(producer))
    await publisher.publish("t.events", "tenant-1", {"b": 2, "a": 1})
    assert producer.sent == [("t.events", b"tenant-1", b'{"a": 1, "b": 2}')]
    assert json.loads(producer.sent[0][2]) == {"a": 1, "b": 2}


async def test_the_producer_starts_once_and_not_before_the_first_publish() -> None:
    producer = FakeProducer()
    publisher = KafkaTenantEventPublisher(_factory(producer))
    assert producer.started == 0
    await publisher.publish("t", "k", {})
    await publisher.publish("t", "k2", {})
    assert producer.started == 1
    await publisher.close()
    assert producer.stopped == 1


async def test_a_missing_ack_raises_so_the_row_is_not_marked() -> None:
    publisher = KafkaTenantEventPublisher(_factory(FakeProducer(fail=True)))
    with pytest.raises(TimeoutError):
        await publisher.publish("t", "k", {})


async def test_close_without_a_publish_stops_nothing() -> None:
    producer = FakeProducer()
    await KafkaTenantEventPublisher(_factory(producer)).close()
    assert producer.stopped == 0
