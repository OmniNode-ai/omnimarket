# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger host takes its subscriptions together, not one group join after another (OMN-20275).

Measured on the dev bus: the serve process's consumers rejoined their groups one at a time, and a
writer late in name order was served 36 to 120 seconds after the first, so a request waited for its
writer's group join. Each group joins independently, so the host starts every join at once and the
whole set is taken in about the time of the slowest.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from omnimarket.nodes.node_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerAppendCommand,
    LocalLedgerFile,
)
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendHost

pytestmark = pytest.mark.unit

Callback = Callable[[Any], Awaitable[None]]
JOIN_S = 0.1
WRITERS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]


class _SlowJoinBus:
    """A bus whose every group join takes ``JOIN_S``, like a broker holding the join open."""

    def __init__(self) -> None:
        self.missing: set[str] = set()
        self.live: dict[str, Callback] = {}

    async def subscribe(
        self, topic: str, on_message: Callback, group_id: str
    ) -> Callable[[], Awaitable[None]]:
        await asyncio.sleep(JOIN_S)
        if topic in self.missing:
            raise TimeoutError(f"Timeout starting consumer for topic {topic}")
        self.live[topic] = on_message

        async def unsubscribe() -> None:
            self.live.pop(topic, None)

        return unsubscribe

    async def get_readiness_status(self) -> SimpleNamespace:
        return SimpleNamespace(
            assignments={topic: [0] for topic in self.live},
            consume_tasks_alive=dict.fromkeys(self.live, True),
        )


def _rig(tmp_path: Path) -> tuple[_SlowJoinBus, WorkLedgerAppendHost]:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("# Test ledger\n", encoding="utf-8")
    runner = LocalLedgerAppendCommand(["true", str(ledger)])
    handler = HandlerWorkLedgerAppendEffect(
        runner, LocalLedgerFile(ledger), "ledger", writer_principals=WRITERS
    )
    bus = _SlowJoinBus()
    return bus, WorkLedgerAppendHost(cast(Any, bus), handler, writer_principals=WRITERS)


def test_start_takes_every_subscription_in_one_join_time(tmp_path: Path) -> None:
    async def scenario() -> float:
        bus, host = _rig(tmp_path)
        try:
            started = time.monotonic()
            await host.start()
            elapsed = time.monotonic() - started
            assert set(bus.live) == set(host.serving_topics)
            return elapsed
        finally:
            await host.stop()

    elapsed = asyncio.run(scenario())
    # Seven joins taken one after another cost 0.7 s; together they cost about one join.
    assert elapsed < JOIN_S * 3, f"{elapsed:.2f}s for {len(WRITERS) + 1} joins"


def test_resubscribe_retakes_every_subscription_in_one_join_time(
    tmp_path: Path,
) -> None:
    async def scenario() -> float:
        bus, host = _rig(tmp_path)
        try:
            await host.start()
            started = time.monotonic()
            await host.resubscribe()
            elapsed = time.monotonic() - started
            assert set(bus.live) == set(host.serving_topics)
            return elapsed
        finally:
            await host.stop()

    elapsed = asyncio.run(scenario())
    assert elapsed < JOIN_S * 3, f"{elapsed:.2f}s for {len(WRITERS) + 1} joins"


def test_a_missing_writer_topic_is_still_skipped_when_joins_overlap(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        bus, host = _rig(tmp_path)
        charlie = host.topics.writer_command_topic("charlie")
        bus.missing.add(charlie)
        try:
            await host.start()
            assert host.unserved_writer_topics == (charlie,)
            assert charlie not in bus.live
            assert len(bus.live) == len(WRITERS)
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_a_missing_shared_command_topic_still_leaves_no_subscription(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        bus, host = _rig(tmp_path)
        bus.missing.add(host.topics.command)
        try:
            with pytest.raises(TimeoutError):
                await host.start()
            assert not bus.live
        finally:
            await host.stop()

    asyncio.run(scenario())
