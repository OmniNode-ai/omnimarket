# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger writer keeps appending across a broker restart that drops its group (OMN-20275).

2026-10-27 the dev broker restarted twice; the writer's consumers logged the coordinator and
session-expired errors and never appended again until the process was kicked. The fake bus below
reproduces that shape: the session expires mid-consume, the coordinator is unavailable, then it returns.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aiokafka.errors import GroupCoordinatorNotAvailableError, UnknownMemberIdError
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import event_type_for
from omnimarket.handlers.work_ledger_consume.consume_watch import (
    ALARM_CLASS,
    ConsumeWatch,
    ModelConsumeWatchPolicy,
    load_consume_watch_policy,
    readiness_probe,
)
from omnimarket.lab_work.bus import _bytes
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerAppendCommand,
    LocalLedgerFile,
)
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendHost

pytestmark = pytest.mark.unit

Callback = Callable[[Any], Awaitable[None]]


class _Handler:
    host_name = "ledger"

    def __init__(self) -> None:
        self.calls: list[UUID] = []

    def handle(
        self,
        request: ModelWorkLedgerAppendRequest,
        *,
        writer_principal: str | None = None,
    ) -> ModelWorkLedgerAppendReceipt:
        self.calls.append(request.request_id)
        return ModelWorkLedgerAppendReceipt(
            request_id=request.request_id,
            status=EnumWorkLedgerAppendStatus.ACCEPTED,
            exit_code=0,
            message="appended",
            ledger_lines=[len(self.calls)],
            ledger_host="ledger",
            duration_ms=1,
        )


class _BrokerBus:
    """A broker whose group coordinator can be down and whose consumers can lose their session.

    A record published while no consumer holds the topic stays in the log and is delivered to the next
    subscription, as a group resuming from its committed offset would receive it.
    """

    def __init__(self) -> None:
        self.coordinator_up = True
        self.log: dict[str, list[bytes]] = {}
        self.delivered: dict[str, int] = {}
        self.live: dict[str, Callback] = {}
        self.subscribe_errors: list[str] = []

    async def subscribe(
        self, topic: str, on_message: Callback, group_id: str
    ) -> Callable[[], Awaitable[None]]:
        if not self.coordinator_up:
            self.subscribe_errors.append(topic)
            raise GroupCoordinatorNotAvailableError()
        self.live[topic] = on_message
        for value in self.log.get(topic, [])[self.delivered.get(topic, 0) :]:
            await self._deliver(topic, value)

        async def unsubscribe() -> None:
            self.live.pop(topic, None)

        return unsubscribe

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> None:
        self.log.setdefault(topic, []).append(value)
        if topic in self.live:
            await self._deliver(topic, value)

    async def _deliver(self, topic: str, value: bytes) -> None:
        self.delivered[topic] = self.delivered.get(topic, 0) + 1
        await self.live[topic](SimpleNamespace(value=value))

    def expire_session(self) -> None:
        """Heartbeat session expired: the member is evicted and holds no partitions."""
        self.coordinator_up = False
        self.live.clear()
        self.errors = [UnknownMemberIdError(), GroupCoordinatorNotAvailableError()]

    async def get_readiness_status(self) -> SimpleNamespace:
        return SimpleNamespace(
            assignments={topic: [0] for topic in self.live},
            consume_tasks_alive=dict.fromkeys(self.live, True),
        )


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


POLICY = ModelConsumeWatchPolicy(
    probe_interval_seconds=1,
    unhealthy_probes_before_degraded=2,
    resubscribe_timeout_seconds=5,
    backoff_initial_seconds=10,
    backoff_factor=2,
    backoff_cap_seconds=30,
)


def _command(host: WorkLedgerAppendHost) -> tuple[str, UUID, bytes]:
    request_id = uuid4()
    request = ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"2026-10-27T05:00:00Z | STATUS | req={request_id}",
        requested_by_lane="lane",
        requesting_host="mac",
        requested_at=datetime.now(UTC),
    )
    topic = host.topics.writer_command_topic("developer")
    envelope = ModelEventEnvelope[dict[str, object]](
        payload=request.model_dump(mode="json"),
        correlation_id=request_id,
        event_type=event_type_for(topic),
    )
    return topic, request_id, _bytes(envelope)


def _rig(
    tmp_path: Path,
) -> tuple[_BrokerBus, _Handler, WorkLedgerAppendHost, ConsumeWatch, _Clock]:
    bus = _BrokerBus()
    handler = _Handler()
    host = WorkLedgerAppendHost(
        cast(Any, bus),
        handler,
        writer_principals=["developer"],
        operator_principal="operator",
    )
    clock = _Clock()
    watch = ConsumeWatch(
        host,
        readiness_probe(bus, lambda: host.serving_topics),
        POLICY,
        host_name="h200",
        state_dir=tmp_path,
        clock=clock,
        now=lambda: datetime(2026, 10, 27, 5, 0, tzinfo=UTC),
    )
    return bus, handler, host, watch, clock


def test_writer_resumes_appending_after_coordinator_and_session_errors(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        bus, handler, host, watch, clock = _rig(tmp_path)
        await host.start()
        try:
            topic, first, value = _command(host)
            await bus.publish(topic, None, value)
            assert handler.calls == [first]

            bus.expire_session()
            _, second, value = _command(host)
            await bus.publish(topic, None, value)
            assert handler.calls == [first]  # nobody is consuming

            # The coordinator is still down: every attempt fails and the watch carries on.
            for _ in range(3):
                await watch.tick()
            assert watch.degraded
            assert bus.subscribe_errors
            assert handler.calls == [first]

            # The broker returns; the next attempt is allowed once the backoff has run.
            bus.coordinator_up = True
            clock.now += POLICY.backoff_initial_seconds
            await watch.tick()
            await host.drain()
            assert handler.calls == [first, second]  # no restart, no lost command

            await watch.tick()
            assert not watch.degraded
            _, third, value = _command(host)
            await bus.publish(topic, None, value)
            assert handler.calls == [first, second, third]
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_dropped_consumer_resubscribes_and_appends_post_drop_rows(
    tmp_path: Path,
) -> None:
    """Five lost sessions recover through the real handler and configured append command."""

    async def scenario() -> None:
        ledger = tmp_path / "ledger.md"
        ledger.write_text("# Test ledger\n", encoding="utf-8")
        runner = LocalLedgerAppendCommand(
            [
                sys.executable,
                "-c",
                "import pathlib, sys; "
                "path = pathlib.Path(sys.argv[1]); "
                "assert sys.argv[2] == '--append'; "
                "path.open('a').write(sys.argv[3] + '\\n')",
                str(ledger),
            ]
        )
        handler = HandlerWorkLedgerAppendEffect(
            runner, LocalLedgerFile(ledger), "ledger", writer_principals=["developer"]
        )
        bus = _BrokerBus()
        host = WorkLedgerAppendHost(
            cast(Any, bus), handler, writer_principals=["developer"]
        )
        clock = _Clock()
        watch = ConsumeWatch(
            host,
            readiness_probe(bus, lambda: host.serving_topics),
            POLICY,
            host_name="h201",
            state_dir=tmp_path,
            clock=clock,
        )
        await host.start()
        worker = host._task
        try:
            await watch.tick()  # establish the healthy assignments before the drop
            for episode in range(5):
                before = ledger.read_bytes()
                bus.expire_session()
                topic, request_id, value = _command(host)
                await bus.publish(topic, None, value)
                assert ledger.read_bytes() == before

                for _ in range(POLICY.unhealthy_probes_before_degraded):
                    await watch.tick()
                assert ledger.read_bytes() == before
                bus.coordinator_up = True
                clock.now += POLICY.backoff_initial_seconds
                await watch.tick()
                await host.drain()

                rows = ledger.read_text().splitlines()[1:]
                assert len(rows) == episode + 1
                assert sum(f"req={request_id}" in row for row in rows) == 1
                assert " | principal=developer | " in rows[-1]
                assert host._task is worker
                assert set(bus.live) == set(host.serving_topics)
                # Redelivery after recovery still returns the original line without appending.
                await bus.publish(topic, None, value)
                assert ledger.read_text().splitlines()[1:] == rows
                await watch.tick()
                assert not watch.degraded
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_backoff_grows_to_the_declared_cap(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus, _, host, watch, clock = _rig(tmp_path)
        await host.start()
        try:
            bus.expire_session()
            attempts: list[float] = []
            for _ in range(12):
                before = watch.resubscribe_attempts
                await watch.tick()
                if watch.resubscribe_attempts > before:
                    attempts.append(clock.now)
                clock.now += 5
            gaps = [b - a for a, b in pairwise(attempts)]
            assert gaps[:2] == [10, 20]
            assert max(gaps) <= POLICY.backoff_cap_seconds + 5
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_degraded_report_and_one_alarm_per_episode(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus, _, host, watch, clock = _rig(tmp_path)
        await host.start()
        try:
            queue = tmp_path / "exception-queue" / "exception-queue.jsonl"
            state = tmp_path / "work-ledger-consume" / "h200.json"

            bus.expire_session()
            await watch.tick()
            assert not watch.degraded  # one bad reading is not an episode
            for _ in range(6):
                await watch.tick()
                clock.now += 1
            assert watch.degraded
            assert json.loads(state.read_text())["state"] == "degraded"
            assert len(watch.alarms) == 1
            rows = [json.loads(line) for line in queue.read_text().splitlines()]
            assert [r["cls"] for r in rows] == [ALARM_CLASS]

            bus.coordinator_up = True
            clock.now += 100
            await watch.tick()
            await watch.tick()
            assert not watch.degraded
            assert json.loads(state.read_text())["state"] == "healthy"

            bus.expire_session()
            for _ in range(3):
                await watch.tick()
            assert len(watch.alarms) == 2  # a second episode is a second alarm
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_policy_comes_from_the_node_contract() -> None:
    policy = load_consume_watch_policy()
    assert policy.backoff_cap_seconds >= policy.backoff_initial_seconds
    assert policy.unhealthy_probes_before_degraded >= 1
