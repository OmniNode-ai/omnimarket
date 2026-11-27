# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25912: the sync snapshot seam creates one Kafka client, not one per call.

``KafkaSnapshotDeltaPublisher.publish`` used to open a ``KafkaTransport``, send
one record and close it inside a fresh ``asyncio.run`` for every delta. Each
open authenticates every broker connection over SCRAM again: the shared dev
runtime logged 1215 ``aiokafka.conn: Authenticated`` lines in 9 minutes
(11:17 to 11:26Z, 2026-11-26, h201), none of them tied to a request or a tick.
The transport now lives as long as the publisher, on one background loop.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from typing import Any
from unittest.mock import patch

import pytest

from omnimarket.projection.snapshot_publisher import (
    KafkaSnapshotDeltaPublisher,
    ModelSnapshotDeltaMessage,
)

_TRANSPORT = "omnibase_infra.event_bus.kafka_transport.KafkaTransport"
_TOPIC = "onex.snapshot.projection.work.events.v1"


def _message(n: int = 1) -> ModelSnapshotDeltaMessage:
    return ModelSnapshotDeltaMessage(
        topic=_TOPIC, key=f'{{"id":"{n}"}}'.encode(), value=b"{}", headers=()
    )


class _FakeTransport:
    """Counts constructions, starts, sends and closes; binds to its start loop."""

    built = 0
    started = 0
    closed = 0
    sent = 0
    fail_start = False
    fail_send = False
    start_loops: list[asyncio.AbstractEventLoop] = []
    built_with: list[tuple[str, tuple[str, ...]]] = []

    @classmethod
    def reset(cls) -> None:
        cls.built = cls.started = cls.closed = cls.sent = 0
        cls.fail_start = cls.fail_send = False
        cls.start_loops = []
        cls.built_with = []

    @classmethod
    def from_bootstrap(
        cls, bootstrap: str, *, topics: tuple[str, ...]
    ) -> _FakeTransport:
        cls.built += 1
        cls.built_with.append((bootstrap, topics))
        return cls()

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        if type(self).fail_start:
            raise RuntimeError("broker unreachable")
        type(self).started += 1
        self._loop = asyncio.get_running_loop()
        type(self).start_loops.append(self._loop)

    async def send(
        self, topic: str, *, key: bytes, value: bytes, headers: Mapping[str, Any]
    ) -> None:
        # A real producer is bound to the loop that started it.
        assert asyncio.get_running_loop() is self._loop
        if type(self).fail_send:
            raise RuntimeError("send failed")
        type(self).sent += 1

    async def close(self) -> None:
        type(self).closed += 1


@pytest.fixture(autouse=True)
def fake_transport() -> Any:
    _FakeTransport.reset()
    with patch(_TRANSPORT, _FakeTransport):
        yield _FakeTransport


def _publisher() -> KafkaSnapshotDeltaPublisher:
    return KafkaSnapshotDeltaPublisher(bootstrap_servers="broker:9092")


@pytest.mark.unit
def test_many_publishes_build_and_start_one_client(fake_transport: Any) -> None:
    publisher = _publisher()
    try:
        for n in range(5):
            assert publisher.publish(_message(n)) is True
        assert fake_transport.sent == 5
        assert fake_transport.built_with == [("broker:9092", ())]
        assert fake_transport.built == 1
        assert fake_transport.started == 1
    finally:
        publisher.close()


@pytest.mark.unit
def test_publishes_from_different_worker_threads_share_one_client(
    fake_transport: Any,
) -> None:
    publisher = _publisher()
    results: list[bool] = []

    def _work(n: int) -> None:
        results.append(publisher.publish(_message(n)))

    threads = [threading.Thread(target=_work, args=(n,)) for n in range(6)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results == [True] * 6
        assert fake_transport.built == 1
        assert fake_transport.started == 1
    finally:
        publisher.close()


@pytest.mark.unit
def test_a_failed_send_drops_the_client_and_the_next_publish_builds_one(
    fake_transport: Any,
) -> None:
    publisher = _publisher()
    try:
        assert publisher.publish(_message(1)) is True
        fake_transport.fail_send = True
        assert publisher.publish(_message(2)) is False
        fake_transport.fail_send = False
        assert publisher.publish(_message(3)) is True
    finally:
        publisher.close()
    assert fake_transport.built == 2
    assert fake_transport.started == 2
    assert fake_transport.closed >= 1


@pytest.mark.unit
def test_a_failed_start_is_retried_by_the_next_publish(fake_transport: Any) -> None:
    publisher = _publisher()
    try:
        fake_transport.fail_start = True
        assert publisher.publish(_message(1)) is False
        fake_transport.fail_start = False
        assert publisher.publish(_message(2)) is True
    finally:
        publisher.close()
    assert fake_transport.started == 1
    assert fake_transport.sent == 1


@pytest.mark.unit
def test_close_stops_the_client_once(fake_transport: Any) -> None:
    publisher = _publisher()
    assert publisher.publish(_message()) is True
    publisher.close()
    publisher.close()
    assert fake_transport.closed == 1


@pytest.mark.unit
def test_a_running_loop_is_still_refused_before_any_client_exists(
    fake_transport: Any,
) -> None:
    from omnimarket.projection.snapshot_publisher import (
        SnapshotPublishFromRunningLoopError,
    )

    publisher = _publisher()

    async def _inside_loop() -> None:
        with pytest.raises(SnapshotPublishFromRunningLoopError):
            publisher.publish(_message())

    asyncio.run(_inside_loop())
    assert fake_transport.built == 0
