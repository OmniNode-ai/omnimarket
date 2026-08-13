# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for ``KafkaEventPublisher`` (OMN-15987).

OMN-15987 finding 1: ``KafkaEventPublisher.publish()`` used to wrap its
async work in ``asyncio.run()``, which raises ``RuntimeError: asyncio.run()
cannot be called from a running event loop`` when ``handle()`` is invoked
from inside a running loop -- the shape canonical dispatch
(``RuntimeDispatch._invoke`` / ``NodeEffect._execute``) actually uses. The
failure was swallowed by ``_try_publish``'s broad ``except Exception`` into
``published=False``, indistinguishable from documented spool-only mode.
``KafkaEventPublisher`` had zero test coverage before this file.

These tests exercise the REAL ``KafkaEventPublisher`` class (real
threading/asyncio plumbing, real ``_publish_async``/``_publish_once``
control flow) with a fake in-process broker injected via the
``bus_factory`` seam -- no real Kafka connection, no mocks of
``KafkaEventPublisher`` itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
    KafkaEventPublisher,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox

pytestmark = pytest.mark.unit


class FakeAsyncBroker:
    """Minimal fake standing in for EventBusKafka's async lifecycle.

    Implements exactly the surface ``KafkaEventPublisher._publish_once``
    calls: ``start()``, ``publish(topic=, key=, value=, headers=)``,
    ``close()``. No network, no real Kafka client.
    """

    def __init__(self, *, fail: bool = False, hang_seconds: float = 0.0) -> None:
        self.started = False
        self.closed = False
        self.published: list[tuple[str, bytes | None, bytes, Any]] = []
        self._fail = fail
        self._hang_seconds = hang_seconds

    async def start(self) -> None:
        self.started = True

    async def publish(
        self, *, topic: str, key: bytes | None, value: bytes, headers: Any
    ) -> None:
        if self._hang_seconds:
            await asyncio.sleep(self._hang_seconds)
        if self._fail:
            raise RuntimeError("simulated broker failure")
        self.published.append((topic, key, value, headers))

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# publish() from a plain sync context (no running loop) -- must still work.
# ---------------------------------------------------------------------------


def test_publish_from_sync_context_succeeds() -> None:
    broker = FakeAsyncBroker()
    publisher = KafkaEventPublisher("fake-bootstrap:9092", bus_factory=lambda: broker)

    publisher.publish(
        "onex.evt.omnimarket.event-emit-completed.v1",
        {"hello": "world"},
        key=None,
        correlation_id=None,
        timeout_seconds=2.0,
    )

    assert broker.started is True
    assert broker.closed is True
    assert len(broker.published) == 1
    topic, _key, value, headers = broker.published[0]
    assert topic == "onex.evt.omnimarket.event-emit-completed.v1"
    assert value == b'{"hello": "world"}'
    assert headers.correlation_id is not None


# ---------------------------------------------------------------------------
# publish() from INSIDE a running event loop -- the exact shape canonical
# dispatch uses, and the exact case that raised RuntimeError before the fix.
# ---------------------------------------------------------------------------


async def test_publish_from_inside_running_loop_succeeds() -> None:
    """This test function is itself a coroutine driven by a running event
    loop (asyncio_mode = auto). Calling the *synchronous*
    ``KafkaEventPublisher.publish()`` from here reproduces exactly the
    canonical-dispatch shape (RuntimeDispatch._invoke calls a sync def-B
    handler's handle() synchronously from inside its own running loop).

    Before the fix: ``asyncio.run()`` inside ``publish()`` raised
    ``RuntimeError: asyncio.run() cannot be called from a running event
    loop``. After the fix: the background dedicated-thread loop makes the
    calling thread's loop state irrelevant.
    """
    broker = FakeAsyncBroker()
    publisher = KafkaEventPublisher("fake-bootstrap:9092", bus_factory=lambda: broker)

    # No RuntimeError -- this is the regression assertion.
    publisher.publish(
        "onex.evt.omnimarket.event-emit-completed.v1",
        {"in_loop": True},
        key="partition-key",
        correlation_id="11111111-1111-1111-1111-111111111111",
        timeout_seconds=2.0,
    )

    assert broker.started is True
    assert broker.closed is True
    assert len(broker.published) == 1
    topic, key, _value, headers = broker.published[0]
    assert topic == "onex.evt.omnimarket.event-emit-completed.v1"
    assert key == b"partition-key"
    assert str(headers.correlation_id) == "11111111-1111-1111-1111-111111111111"


async def test_handler_handle_from_inside_running_loop_publishes(
    tmp_path: Path,
) -> None:
    """End-to-end: HandlerEventEmitEffect.handle() -- the sync def-B handler
    canonical dispatch calls from inside a running loop -- wired to the
    REAL KafkaEventPublisher (fake broker injected), driven from inside a
    running event loop. Asserts published=True and no RuntimeError,
    matching OMN-15987's required regression coverage exactly.
    """
    broker = FakeAsyncBroker()
    publisher = KafkaEventPublisher("fake-bootstrap:9092", bus_factory=lambda: broker)
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=publisher)

    request = ModelEmitRequest(
        event_type="session.started", payload={"session_id": "in-loop"}
    )
    result = handler.handle(request)

    assert result.published is True
    assert result.spool_only is False
    assert result.topics_published == ["onex.evt.omniclaude.session-started.v1"]
    assert len(broker.published) == 1
    assert spool.pending_count() == 0


async def test_publish_broker_failure_inside_running_loop_propagates_as_failure() -> (
    None
):
    """A genuine broker failure (not the asyncio.run() wiring bug) must
    still surface as a normal publish failure -- proving the fix didn't
    turn broker errors into silent successes."""
    broker = FakeAsyncBroker(fail=True)
    publisher = KafkaEventPublisher("fake-bootstrap:9092", bus_factory=lambda: broker)

    with pytest.raises(RuntimeError, match="simulated broker failure"):
        publisher.publish(
            "onex.evt.omnimarket.event-emit-completed.v1",
            {},
            key=None,
            correlation_id=None,
            timeout_seconds=2.0,
        )
    assert broker.closed is True  # finally block still closes the bus


async def test_publish_timeout_inside_running_loop_raises_timeout() -> None:
    """A hung broker call is bounded by timeout_seconds, not left to hang
    forever -- exercised from inside a running loop."""
    broker = FakeAsyncBroker(hang_seconds=5.0)
    publisher = KafkaEventPublisher("fake-bootstrap:9092", bus_factory=lambda: broker)

    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        publisher.publish(
            "onex.evt.omnimarket.event-emit-completed.v1",
            {},
            key=None,
            correlation_id=None,
            timeout_seconds=0.2,
        )
