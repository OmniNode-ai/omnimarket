# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Kafka connection reuse across an emit and its backlog drain (OMN-17427)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
    KafkaEventPublisher,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    JsonType,
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox

pytestmark = pytest.mark.unit


class CountingAsyncBroker:
    """Count the async lifecycle calls without connecting to Kafka."""

    def __init__(
        self,
        *,
        publish_error: Exception | None = None,
        start_error: Exception | None = None,
        close_error: Exception | None = None,
        start_delay: float = 0.0,
        publish_delay: float = 0.0,
    ) -> None:
        self.start_count = 0
        self.close_count = 0
        self.published: list[tuple[str, bytes | None, bytes, Any]] = []
        self.publish_error = publish_error
        self.start_error = start_error
        self.close_error = close_error
        self.start_delay = start_delay
        self.publish_delay = publish_delay

    async def start(self) -> None:
        self.start_count += 1
        if self.start_delay:
            await asyncio.sleep(self.start_delay)
        if self.start_error is not None:
            raise self.start_error

    async def publish(
        self, *, topic: str, key: bytes | None, value: bytes, headers: Any
    ) -> None:
        if self.publish_delay:
            await asyncio.sleep(self.publish_delay)
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append((topic, key, value, headers))

    async def close(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


class CountingBusFactory:
    def __init__(self, **first_bus_options: Any) -> None:
        self.buses: list[CountingAsyncBroker] = []
        self.first_bus_options = first_bus_options

    def __call__(self) -> CountingAsyncBroker:
        bus = CountingAsyncBroker(**self.first_bus_options if not self.buses else {})
        self.buses.append(bus)
        return bus


class FailingPublishAdapter:
    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
        content_event_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        raise RuntimeError("simulated broker failure")


def test_session_reuses_one_bus_for_three_publishes() -> None:
    factory = CountingBusFactory()
    publisher = KafkaEventPublisher("fake-bootstrap:9092", bus_factory=factory)

    with publisher.session():
        assert factory.buses == []
        for index in range(3):
            publisher.publish(
                f"topic-{index}",
                {"index": index},
                key=str(index),
                correlation_id=None,
            )
            assert len(factory.buses) == 1
            assert factory.buses[0].close_count == 0
        bus = factory.buses[0]
        assert bus.start_count == 1
        assert [record[0] for record in bus.published] == [
            "topic-0",
            "topic-1",
            "topic-2",
        ]
        assert [json.loads(record[2]) for record in bus.published] == [
            {"index": index} for index in range(3)
        ]

    assert bus.close_count == 1


def test_plain_publishes_each_build_and_close_a_bus() -> None:
    factory = CountingBusFactory()
    publisher = KafkaEventPublisher("fake-bootstrap:9092", bus_factory=factory)

    for index in range(2):
        publisher.publish("topic", {"index": index}, key=None, correlation_id=None)
        assert len(factory.buses) == index + 1
        assert factory.buses[-1].close_count == 1

    assert [bus.start_count for bus in factory.buses] == [1, 1]
    assert [bus.close_count for bus in factory.buses] == [1, 1]


def test_publish_failure_discards_session_bus_and_preserves_exception() -> None:
    error = RuntimeError("simulated broker failure")
    factory = CountingBusFactory(publish_error=error)
    publisher = KafkaEventPublisher("fake-bootstrap:9092", bus_factory=factory)

    with publisher.session():
        with pytest.raises(RuntimeError) as exc_info:
            publisher.publish("topic", {}, key=None, correlation_id=None)
        assert exc_info.value is error
        assert factory.buses[0].close_count == 1

        publisher.publish("topic", {"retry": True}, key=None, correlation_id=None)
        assert len(factory.buses) == 2
        assert factory.buses[1].close_count == 0
        assert len(factory.buses[1].published) == 1

    assert [bus.close_count for bus in factory.buses] == [1, 1]


@pytest.mark.parametrize("delay_field", ["start_delay", "publish_delay"])
def test_session_publish_timeout_discards_bus(delay_field: str) -> None:
    factory = CountingBusFactory(**{delay_field: 5.0})
    publisher = KafkaEventPublisher("fake-bootstrap:9092", bus_factory=factory)

    with publisher.session():
        with pytest.raises(TimeoutError):
            publisher.publish(
                "topic", {}, key=None, correlation_id=None, timeout_seconds=0.05
            )
        if delay_field == "publish_delay":
            assert factory.buses[0].close_count == 1

        publisher.publish("topic", {}, key=None, correlation_id=None)
        assert len(factory.buses) == 2
        assert factory.buses[1].start_count == 1

    assert factory.buses[1].close_count == 1


def test_start_failure_does_not_cache_bus() -> None:
    error = RuntimeError("simulated start failure")
    factory = CountingBusFactory(start_error=error)
    publisher = KafkaEventPublisher("fake-bootstrap:9092", bus_factory=factory)

    with publisher.session():
        with pytest.raises(RuntimeError) as exc_info:
            publisher.publish("topic", {}, key=None, correlation_id=None)
        assert exc_info.value is error

        publisher.publish("topic", {}, key=None, correlation_id=None)
        assert len(factory.buses) == 2
        assert factory.buses[0].published == []

    assert factory.buses[1].close_count == 1


@pytest.mark.parametrize("publish_fails", [False, True])
def test_session_close_failure_is_logged_and_swallowed(
    caplog: pytest.LogCaptureFixture, publish_fails: bool
) -> None:
    error = RuntimeError("simulated broker failure") if publish_fails else None
    factory = CountingBusFactory(
        publish_error=error, close_error=RuntimeError("simulated close failure")
    )
    publisher = KafkaEventPublisher("fake-bootstrap:9092", bus_factory=factory)

    with publisher.session():
        if publish_fails:
            with pytest.raises(RuntimeError) as exc_info:
                publisher.publish("topic", {}, key=None, correlation_id=None)
            assert exc_info.value is error
            publisher.publish("topic", {}, key=None, correlation_id=None)
        else:
            publisher.publish("topic", {}, key=None, correlation_id=None)

    assert all(bus.close_count == 1 for bus in factory.buses)
    assert "Failed to close Kafka publish session" in caplog.text


def test_handler_shares_session_with_backlog_drain(tmp_path: Path) -> None:
    spool = SpoolOutbox(tmp_path / "spool")
    failing_handler = HandlerEventEmitEffect(
        spool=spool, publish_adapter=FailingPublishAdapter()
    )
    for index in range(3):
        result = failing_handler.handle(
            ModelEmitRequest(
                event_type="session.started", payload={"session_id": f"s{index}"}
            )
        )
        assert result.published is False
    assert spool.pending_count() == 3

    factory = CountingBusFactory()
    publisher = KafkaEventPublisher("fake-bootstrap:9092", bus_factory=factory)
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=publisher)

    result = handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"session_id": "s3"})
    )

    assert result.published is True
    assert result.drained_count == 3
    assert spool.pending_count() == 0
    assert len(factory.buses) == 1
    bus = factory.buses[0]
    assert bus.start_count == 1
    assert len(bus.published) == 4
    assert [json.loads(record[2])["session_id"] for record in bus.published] == [
        "s3",
        "s0",
        "s1",
        "s2",
    ]
    assert bus.close_count == 1
