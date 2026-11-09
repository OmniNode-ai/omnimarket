# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23325 -- a stopped runtime-owned producer must never strand the publish callable.

Measured on the dev runtime (2026-11-09): ``node_projection_delegation`` logged
``Failed to publish terminal event ... ProducerClosed`` on every delivery after
the first, and ``LocalEnvironmentProjectionWriter`` wrote no row for the same
reason. ``KeptPools.project`` stops the producer after every delivery, but
``get_publish_fn`` cached a closure over the producer it had just built, so the
next delivery published through a stopped producer for the life of the process.

The fake producer below behaves like aiokafka's: ``send_and_wait`` on a stopped
producer raises ``ProducerClosed``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiokafka.errors import ProducerClosed

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    ModelProjectionRuntimeBinding,
)


class _FakeProducer:
    """Mirrors aiokafka: a stopped producer refuses every send."""

    instances: list[_FakeProducer] = []

    def __init__(self, **_kwargs: Any) -> None:
        self.started = False
        self.sent: list[tuple[str, bytes]] = []
        _FakeProducer.instances.append(self)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def send_and_wait(self, topic: str, value: bytes, **_kwargs: Any) -> None:
        if not self.started:
            raise ProducerClosed()
        self.sent.append((topic, value))


@pytest.fixture(autouse=True)
def _fresh_fake_producers() -> Any:
    _FakeProducer.instances = []
    with patch("omnimarket.projection.runner.AIOKafkaProducer", _FakeProducer):
        yield


class _StubRunner(BaseProjectionRunner):
    @property
    def topics(self) -> list[str]:
        return []

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        return True


def _delegation_runner() -> DelegationProjectionRunner:
    runner = DelegationProjectionRunner()
    runner._runtime_binding = _binding()
    return runner


def _binding() -> ModelProjectionRuntimeBinding:
    return ModelProjectionRuntimeBinding(
        kafka_bootstrap_servers="localhost:9092",
        database_url="postgresql://stub/stub",  # type: ignore[arg-type]
    )


@pytest.mark.unit
def test_producer_closed_terminal_publish_after_producer_stop_uses_a_fresh_producer() -> (
    None
):
    """The delivery after a stop publishes; it must not hit the stopped producer."""

    async def scenario() -> None:
        runner = _StubRunner(runtime_binding=_binding())
        first = await runner.get_publish_fn()
        assert first is not None
        await first("t", b"one")
        await runner._stop_producer()

        second = await runner.get_publish_fn()
        assert second is not None
        await second("t", b"two")

    asyncio.run(scenario())
    assert [p.sent for p in _FakeProducer.instances] == [
        [("t", b"one")],
        [("t", b"two")],
    ]


@pytest.mark.unit
def test_producer_closed_terminal_a_publish_callable_held_across_a_stop_still_publishes() -> (
    None
):
    """A handler that kept the callable from before the stop must not lose the event."""

    async def scenario() -> None:
        runner = _StubRunner(runtime_binding=_binding())
        held = await runner.get_publish_fn()
        assert held is not None
        await runner._stop_producer()
        await held("t", b"after-stop")

    asyncio.run(scenario())
    assert _FakeProducer.instances[-1].sent == [("t", b"after-stop")]


@pytest.mark.unit
def test_producer_closed_terminal_injected_publish_fn_survives_a_producer_stop() -> (
    None
):
    published: list[tuple[str, bytes]] = []

    async def capture(topic: str, value: bytes) -> None:
        published.append((topic, value))

    async def scenario() -> None:
        runner = _StubRunner(runtime_binding=_binding(), publish_fn=capture)
        await runner._stop_producer()
        fn = await runner.get_publish_fn()
        assert fn is capture
        await fn("t", b"x")

    asyncio.run(scenario())
    assert published == [("t", b"x")]
    assert _FakeProducer.instances == []


@pytest.mark.unit
def test_producer_closed_terminal_delegation_terminal_events_survive_every_runtime_delivery() -> (
    None
):
    """Golden chain through the runtime host path: each delivery emits its terminal event.

    Before the fix the second and later deliveries logged ProducerClosed and
    emitted nothing (the 2544 lost terminal events on the dev runtime).
    """

    async def scenario() -> DelegationProjectionRunner:
        runner = _delegation_runner()
        mock_db = MagicMock(spec=AsyncpgAdapter)
        mock_db.execute = AsyncMock(return_value=None)
        runner._db = mock_db
        runner._connect_standalone_databases = AsyncMock()  # type: ignore[method-assign]
        runner._close_standalone_databases = AsyncMock()  # type: ignore[method-assign]
        topic = runner.subscribe_topics[0]
        for n in range(3):
            data = {
                "correlation_id": f"omn23304-c{n}",
                "task_type": "code-review",
                "delegated_to": "agent-alpha",
            }
            meta = MessageMeta(partition=0, offset=n, fallback_id=f"omn23304-c{n}")
            await runner.project_runtime_event(topic, data, meta)
        return runner

    runner = asyncio.run(scenario())
    terminal_topic = runner._terminal_topic
    assert terminal_topic
    sent = [m for p in _FakeProducer.instances for m in p.sent]
    assert [t for t, _ in sent] == [terminal_topic] * 3


@pytest.mark.unit
def test_producer_closed_terminal_delegation_terminal_event_failure_is_still_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Error chain: a broker that really refuses the send is logged and never fails the row."""

    class _RefusingProducer(_FakeProducer):
        async def send_and_wait(self, topic: str, value: bytes, **_k: Any) -> None:
            raise ProducerClosed()

    async def scenario() -> bool:
        runner = _delegation_runner()
        mock_db = MagicMock(spec=AsyncpgAdapter)
        mock_db.execute = AsyncMock(return_value=None)
        runner._db = mock_db
        topic = runner.subscribe_topics[0]
        data = {"correlation_id": "omn23304-err", "task_type": "t", "delegated_to": "a"}
        meta = MessageMeta(partition=0, offset=0, fallback_id="omn23304-err")
        return await runner.project_event(topic, data, meta)

    with (
        patch("omnimarket.projection.runner.AIOKafkaProducer", _RefusingProducer),
        caplog.at_level("WARNING"),
    ):
        assert asyncio.run(scenario()) is True
    assert "Failed to publish terminal event" in caplog.text
