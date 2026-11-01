# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A missing writer topic must not stop the other ledger writers (OMN-21288)."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import event_type_for
from omnimarket.handlers.work_ledger_consume.consume_watch import readiness_probe
from omnimarket.lab_work.bus import _bytes
from omnimarket.nodes.node_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerAppendCommand,
    LocalLedgerFile,
)
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendHost

pytestmark = pytest.mark.unit

Callback = Callable[[Any], Awaitable[None]]


class _BrokerBus:
    def __init__(self) -> None:
        self.missing: set[str] = set()
        self.live: dict[str, Callback] = {}

    async def subscribe(
        self, topic: str, on_message: Callback, group_id: str
    ) -> Callable[[], Awaitable[None]]:
        if topic in self.missing:
            raise TimeoutError(f"Timeout starting consumer for topic {topic}")
        self.live[topic] = on_message

        async def unsubscribe() -> None:
            self.live.pop(topic, None)

        return unsubscribe

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> None:
        if topic in self.live:
            await self.live[topic](SimpleNamespace(value=value))

    async def get_readiness_status(self) -> SimpleNamespace:
        return SimpleNamespace(
            assignments={topic: [0] for topic in self.live},
            consume_tasks_alive=dict.fromkeys(self.live, True),
        )


def _command(host: WorkLedgerAppendHost) -> tuple[str, UUID, bytes]:
    request_id = uuid4()
    request = ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"2026-10-31T02:00:00Z | STATUS | req={request_id}",
        requested_by_lane="lane",
        requesting_host="mac",
        requested_at=datetime.now(UTC),
    )
    topic = host.topics.writer_command_topic("alpha")
    envelope = ModelEventEnvelope[dict[str, object]](
        payload=request.model_dump(mode="json"),
        correlation_id=request_id,
        event_type=event_type_for(topic),
    )
    return topic, request_id, _bytes(envelope)


def _rig(tmp_path: Path) -> tuple[_BrokerBus, WorkLedgerAppendHost, Path]:
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
        runner, LocalLedgerFile(ledger), "ledger", writer_principals=["alpha", "bravo"]
    )
    bus = _BrokerBus()
    host = WorkLedgerAppendHost(
        cast(Any, bus), handler, writer_principals=["alpha", "bravo"]
    )
    return bus, host, ledger


def test_missing_writer_topic_keeps_other_writer_appending(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus, host, ledger = _rig(tmp_path)
        bravo = host.topics.writer_command_topic("bravo")
        bus.missing.add(bravo)
        try:
            await host.start()
            assert host.unserved_writer_topics == (bravo,)
            assert bravo not in bus.live
            topic, request_id, value = _command(host)
            await bus.publish(topic, None, value)
            rows = ledger.read_text().splitlines()[1:]
            assert len(rows) == 1
            assert f"req={request_id}" in rows[0]
            assert " | principal=alpha | " in rows[0]
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_missing_shared_command_topic_is_fatal(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus, host, _ = _rig(tmp_path)
        bus.missing.add(host.topics.command)
        try:
            with pytest.raises(TimeoutError):
                await host.start()
            assert not bus.live
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_resubscribe_retakes_previously_missing_writer_topic(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus, host, _ = _rig(tmp_path)
        bravo = host.topics.writer_command_topic("bravo")
        bus.missing.add(bravo)
        try:
            await host.start()
            assert host.unserved_writer_topics == (bravo,)
            bus.missing.remove(bravo)
            await host.resubscribe()
            assert host.unserved_writer_topics == ()
            assert bravo in bus.live
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_missing_writer_topic_does_not_degrade_readiness(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus, host, _ = _rig(tmp_path)
        bravo = host.topics.writer_command_topic("bravo")
        bus.missing.add(bravo)
        try:
            await host.start()
            assert host.unserved_writer_topics == (bravo,)
            assert bravo in host.serving_topics
            assert await readiness_probe(bus, lambda: host.serving_topics)() == ""
        finally:
            await host.stop()

    asyncio.run(scenario())
