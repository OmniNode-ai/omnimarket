# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: the work-state projection's scheduled tick ran on the runtime's event loop, so its blocking source reads stalled every heartbeat."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _resolve_effective_handle_method,
)

from omnimarket.models.work_state_projection import (
    ModelWorkScope,
    ModelWorkStateRequest,
    ModelWorkStateResult,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from omnimarket.nodes.node_work_state_projection_scheduled_effect.handlers.handler_work_state_projection_scheduled import (
    HandlerWorkStateProjectionScheduled,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import (
    T0,
    rules,
    schedule,
    tick,
)

pytestmark = pytest.mark.unit

BLOCK_SECONDS = 1.0
HEARTBEAT = 0.01
BOUND = BLOCK_SECONDS * 0.5


class _BlockingProjector:
    """The projector the runtime measured: reading the sources blocks its caller for BLOCK_SECONDS."""

    def __init__(self) -> None:
        self.calls = 0

    def project(self, now: datetime) -> ModelWorkStateResult:
        self.calls += 1
        time.sleep(BLOCK_SECONDS)
        return HandlerWorkStateProjection().handle(
            ModelWorkStateRequest(
                now=now,
                rules=rules(),
                scope=ModelWorkScope(in_scope_projects=("Zephyr",)),
                tickets=(),
            )
        )


class _HeldProjector:
    """The projector a test holds: reading the sources blocks until the test releases it."""

    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def project(self, now: datetime) -> ModelWorkStateResult:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=5)
        return HandlerWorkStateProjection().handle(
            ModelWorkStateRequest(
                now=now,
                rules=rules(),
                scope=ModelWorkScope(in_scope_projects=("Zephyr",)),
                tickets=(),
            )
        )


class _MutableClock:
    """The handler's clock a test advances: the age of the in-flight run moves with it."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _measured(call: Callable[[], object]) -> tuple[object, float]:
    """Run one handler call beside a heartbeat on a fresh loop; return its result and the largest gap between beats."""
    return asyncio.run(_beside_a_heartbeat(call))


async def _beside_a_heartbeat(call: Callable[[], object]) -> tuple[object, float]:
    wakeups: list[float] = []
    beating = asyncio.Event()

    async def heartbeat() -> None:
        last = time.monotonic()
        beating.set()
        while True:
            await asyncio.sleep(HEARTBEAT)
            now = time.monotonic()
            wakeups.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await beating.wait()
    await asyncio.sleep(HEARTBEAT * 2)
    try:
        outcome = call()
        if inspect.isawaitable(outcome):
            outcome = await outcome
        # Let the heartbeat wake once more, so a stall that ended with the call is on its record.
        await asyncio.sleep(HEARTBEAT * 3)
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
    return outcome, max(wakeups)


def test_golden_the_loop_keeps_beating_while_the_run_is_in_flight() -> None:
    h = HandlerWorkStateProjectionScheduled(
        schedule=schedule(), projector=_BlockingProjector()
    )
    event, gap = _measured(lambda: h.handle_async(tick(T0)))
    assert event is not None
    assert gap < BOUND


def test_failing_control_the_sync_handle_on_the_loop_blocks_the_heartbeat() -> None:
    h = HandlerWorkStateProjectionScheduled(
        schedule=schedule(), projector=_BlockingProjector()
    )
    event, gap = _measured(lambda: h.handle(tick(T0)))
    assert event is not None
    assert gap >= BLOCK_SECONDS * 0.8


def test_error_a_second_tick_while_a_run_is_in_flight_starts_no_second_run() -> None:
    projector = _BlockingProjector()
    h = HandlerWorkStateProjectionScheduled(schedule=schedule(), projector=projector)

    async def two_ticks() -> list[object]:
        return await asyncio.gather(
            h.handle_async(tick(T0)),
            h.handle_async(tick(T0 + timedelta(seconds=1))),
        )

    results = asyncio.run(two_ticks())
    assert sum(result is not None for result in results) == 1
    assert projector.calls == 1


def test_error_a_skipped_tick_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    projector = _BlockingProjector()
    h = HandlerWorkStateProjectionScheduled(schedule=schedule(), projector=projector)

    async def two_ticks() -> list[object]:
        return await asyncio.gather(
            h.handle_async(tick(T0)),
            h.handle_async(tick(T0 + timedelta(seconds=1))),
        )

    with caplog.at_level(logging.WARNING):
        results = asyncio.run(two_ticks())
    assert sum(result is not None for result in results) == 1
    assert any("tick skipped" in record.message for record in caplog.records)


def test_error_a_run_longer_than_its_interval_fails_the_next_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    held = _HeldProjector()
    clk = _MutableClock()
    h = HandlerWorkStateProjectionScheduled(
        schedule=schedule(), projector=held, clock=clk
    )

    async def held_run_then_a_late_tick() -> object:
        first = asyncio.create_task(h.handle_async(tick(T0)))
        await asyncio.to_thread(held.started.wait, 5)
        clk.advance(400)
        try:
            with pytest.raises(RuntimeError, match="in flight"):
                await h.handle_async(tick(T0 + timedelta(seconds=400)))
        finally:
            held.release.set()
        return await first

    with caplog.at_level(logging.ERROR):
        first = asyncio.run(held_run_then_a_late_tick())
    assert first is not None
    assert held.calls == 1
    assert any("in flight" in record.message for record in caplog.records)


def test_error_cancelling_the_awaiting_task_does_not_release_a_running_worker() -> None:
    held = _HeldProjector()
    h = HandlerWorkStateProjectionScheduled(schedule=schedule(), projector=held)

    async def cancelled_first_run_then_a_late_tick() -> tuple[object, object, int]:
        first = asyncio.create_task(h.handle_async(tick(T0)))
        await asyncio.to_thread(held.started.wait, 5)
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first
        second = await h.handle_async(tick(T0 + timedelta(seconds=400)))
        calls_at_second = held.calls
        held.release.set()
        deadline = time.monotonic() + 2.0
        third: object = None
        while time.monotonic() < deadline:
            third = await h.handle_async(tick(T0 + timedelta(seconds=400)))
            if third is not None:
                break
            await asyncio.sleep(0.01)
        return second, third, calls_at_second

    second, third, calls_at_second = asyncio.run(cancelled_first_run_then_a_late_tick())
    assert second is None
    assert calls_at_second == 1
    assert third is not None
    assert held.calls == 2


def test_seam_the_runtime_wiring_picks_handle_async() -> None:
    h = HandlerWorkStateProjectionScheduled(schedule=schedule())
    resolved = _resolve_effective_handle_method(h)
    assert resolved is not None
    assert getattr(resolved, "__self__", None) is h
    assert (
        getattr(getattr(resolved, "__func__", None), "__name__", "") == "handle_async"
    )
    assert inspect.iscoroutinefunction(resolved)
