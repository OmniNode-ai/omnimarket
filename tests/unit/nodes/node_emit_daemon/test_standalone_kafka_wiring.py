# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Proof that the standalone emit-daemon runner actually wires Kafka publish.

OMN-13144: the standalone runner accepted ``--kafka-bootstrap-servers`` and
logged "Kafka publishing enabled", but left ``publish_fn`` bound to the
``_noop_publish`` stub — every event was acked-and-dropped. These tests pin
the corrected behaviour:

1. ``build_kafka_publish_fn`` routes an event through ``bus.publish`` with the
   canonical typed ``ModelEventHeaders`` (the same path the in-process
   omniclaude lifecycle uses).
2. End-to-end through ``KafkaPublisherLoop``: a queued event reaches the fake
   bus's ``publish`` exactly once, proving the wiring is live rather than a
   no-op.
3. ``create_kafka_bus`` fails fast on an empty bootstrap string instead of
   silently starting a broker-less bus.

No real Kafka is required — a fake bus captures the publish calls. The tests
fail against the pre-fix ``__main__.py`` because that code never built a
publish callable off ``_noop_publish``.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_emit_daemon.event_queue import (
    BoundedEventQueue,
    ModelQueuedEvent,
)
from omnimarket.nodes.node_emit_daemon.kafka_publish import (
    build_kafka_publish_fn,
    create_kafka_bus,
)
from omnimarket.nodes.node_emit_daemon.publisher_loop import KafkaPublisherLoop


class _FakeKafkaBus:
    """Records publish calls so tests can assert events actually reach a bus."""

    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.published: list[dict[str, object]] = []

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object | None = None,
    ) -> None:
        self.published.append(
            {
                "topic": topic,
                "key": key,
                "value": value,
                "headers": headers,
            }
        )


def _make_event(event_id: str = "evt-1") -> ModelQueuedEvent:
    return ModelQueuedEvent(
        event_id=event_id,
        event_type="session.started",
        topic="onex.evt.omniclaude.session-started.v1",
        payload={"session_id": "abc"},
        partition_key="abc",
        queued_at=datetime.now(UTC),
    )


class TestBuildKafkaPublishFn:
    """The publish callable must route through bus.publish with typed headers."""

    @pytest.mark.asyncio
    async def test_publish_fn_calls_bus_publish(self) -> None:
        from omnibase_infra.event_bus.models import ModelEventHeaders

        bus = _FakeKafkaBus()
        publish_fn = build_kafka_publish_fn(bus, source="omnimarket")

        await publish_fn(
            "onex.evt.test.v1",
            b"abc",
            b'{"session_id": "abc"}',
            {
                "source": "omnimarket",
                "event_type": "session.started",
                "timestamp": datetime.now(UTC).isoformat(),
                "correlation_id": "11111111-1111-1111-1111-111111111111",
            },
        )

        assert len(bus.published) == 1
        call = bus.published[0]
        assert call["topic"] == "onex.evt.test.v1"
        assert call["key"] == b"abc"
        assert call["value"] == b'{"session_id": "abc"}'
        headers = call["headers"]
        assert isinstance(headers, ModelEventHeaders)
        assert headers.source == "omnimarket"
        assert headers.event_type == "session.started"
        assert str(headers.correlation_id) == ("11111111-1111-1111-1111-111111111111")

    @pytest.mark.asyncio
    async def test_publish_fn_tolerates_missing_correlation_id(self) -> None:
        from omnibase_infra.event_bus.models import ModelEventHeaders

        bus = _FakeKafkaBus()
        publish_fn = build_kafka_publish_fn(bus, source="omnimarket")

        await publish_fn(
            "onex.evt.test.v1",
            None,
            b"{}",
            {"source": "omnimarket", "event_type": "x"},
        )

        assert len(bus.published) == 1
        headers = bus.published[0]["headers"]
        assert isinstance(headers, ModelEventHeaders)
        # A fresh UUID is minted when the header is absent/invalid.
        assert headers.correlation_id is not None


class TestStandalonePublishesThroughBus:
    """End-to-end: a queued event must reach the bus, not a no-op stub."""

    @pytest.mark.asyncio
    async def test_event_reaches_bus_via_publisher_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            queue = BoundedEventQueue(spool_dir=Path(tmpdir))
            bus = _FakeKafkaBus()

            # This is exactly what _do_start does once the bus is started:
            # rebind the publisher's publish callable off the no-op stub.
            publisher = KafkaPublisherLoop(
                queue=queue,
                publish_fn=_noop_sentinel,
            )
            publisher.set_publish_fn(build_kafka_publish_fn(bus, source="omnimarket"))

            await queue.enqueue(_make_event())
            await publisher.start()
            await asyncio.sleep(0.3)
            await publisher.stop()

            assert publisher.events_published == 1
            assert len(bus.published) == 1, (
                "event was acked but never reached the bus — the standalone "
                "Kafka publish path is unwired (OMN-13144 regression)"
            )
            payload = json.loads(bus.published[0]["value"])  # type: ignore[arg-type]
            assert payload["session_id"] == "abc"


class TestCreateKafkaBusFailFast:
    """An empty bootstrap string must fail fast, never start a broker-less bus."""

    @pytest.mark.asyncio
    async def test_empty_bootstrap_raises(self) -> None:
        with pytest.raises(ValueError, match="bootstrap_servers is required"):
            await create_kafka_bus("", timeout_seconds=10.0)


class TestWireKafkaPublisher:
    """``__main__._wire_kafka_publisher`` is the seam that fixes the dead path.

    Pre-fix, the ``__main__`` start path only logged "Kafka publishing enabled"
    and left ``publish_fn`` bound to the no-op stub. These tests pin that the
    boundary now (a) builds + starts a real bus, (b) rebinds the publisher's
    publish callable so events flow through ``bus.publish``, and (c) returns
    ``None`` (spool-only) when no bootstrap servers are supplied.
    """

    @pytest.mark.asyncio
    async def test_wires_real_publish_when_bootstrap_supplied(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from omnimarket.nodes.node_emit_daemon import __main__ as main_mod

        bus = _FakeKafkaBus()

        async def _fake_create_bus(
            bootstrap_servers: str, *, timeout_seconds: float
        ) -> object:
            assert bootstrap_servers == "localhost:9092"
            await bus.start()
            return bus

        monkeypatch.setattr(main_mod, "create_kafka_bus", _fake_create_bus)

        queue = BoundedEventQueue(spool_dir=tmp_path)
        publisher = KafkaPublisherLoop(queue=queue, publish_fn=_noop_sentinel)

        returned = await main_mod._wire_kafka_publisher(publisher, "localhost:9092")

        assert returned is bus
        assert bus.started is True

        # The publisher must now route through the real bus, not the sentinel.
        await queue.enqueue(_make_event())
        await publisher.start()
        await asyncio.sleep(0.3)
        await publisher.stop()

        assert publisher.events_published == 1
        assert len(bus.published) == 1, (
            "event acked but never reached the bus — __main__ left the Kafka "
            "publish path unwired (OMN-13144 regression)"
        )
        payload = json.loads(bus.published[0]["value"])  # type: ignore[arg-type]
        assert payload["session_id"] == "abc"

    @pytest.mark.asyncio
    async def test_spool_only_when_no_bootstrap(self, tmp_path: Path) -> None:
        from omnimarket.nodes.node_emit_daemon import __main__ as main_mod

        queue = BoundedEventQueue(spool_dir=tmp_path)
        publisher = KafkaPublisherLoop(queue=queue, publish_fn=_noop_sentinel)

        returned = await main_mod._wire_kafka_publisher(publisher, None)

        assert returned is None
        # publish_fn was left untouched (still the sentinel) — spool-only mode.
        assert publisher._publish_fn is _noop_sentinel


async def _noop_sentinel(
    topic: str,
    key: bytes | None,
    value: bytes,
    headers: dict[str, str],
) -> None:
    """Stand-in that MUST be replaced by set_publish_fn before the loop runs.

    If wiring is broken and this is still active, the bus never sees a publish
    and the end-to-end assertion fails — pinning the OMN-13144 regression.
    """
    raise AssertionError(
        "publisher used the no-op sentinel instead of the wired Kafka publish_fn"
    )
