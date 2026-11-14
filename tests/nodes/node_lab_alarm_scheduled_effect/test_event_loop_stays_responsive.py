# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: the lab alarm's scheduled tick ran on the runtime's event loop, so its blocking run stalled every heartbeat."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _resolve_effective_handle_method,
)
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.lab_alarm_scheduled_tick import ModelLabAlarmTickOutcome
from omnimarket.nodes.node_lab_alarm_scheduled_effect.handlers.handler_lab_alarm_scheduled import (
    HandlerLabAlarmScheduled,
)

pytestmark = pytest.mark.unit

BLOCK_SECONDS = 1.0
HEARTBEAT = 0.01
BOUND = BLOCK_SECONDS * 0.5

NODE = "node_lab_alarm_scheduled_effect"
ALARM_NODE = "node_lab_alarm"
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
DONE = ModelLabAlarmTickOutcome(False, "outcome=ok declared=1")


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def environ(tmp_path: Path) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "lab_alarm",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
                "alarm_overlay_id": ALARM_NODE,
            }
        ),
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


class _BlockingTick:
    """The tick the runtime measured: the alarm run blocks its caller for BLOCK_SECONDS."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(
        self,
        *,
        alarm_overlay_id: str,
        environ: Mapping[str, str] | None,
        clock: Callable[[], datetime],
        build_handler: Callable[..., Any],
    ) -> ModelLabAlarmTickOutcome:
        self.calls.append(alarm_overlay_id)
        time.sleep(BLOCK_SECONDS)
        return DONE


class _HeldTick:
    """The tick a test holds: the alarm run blocks until the test releases it."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(
        self,
        *,
        alarm_overlay_id: str,
        environ: Mapping[str, str] | None,
        clock: Callable[[], datetime],
        build_handler: Callable[..., Any],
    ) -> ModelLabAlarmTickOutcome:
        self.calls.append(alarm_overlay_id)
        self.started.set()
        self.release.wait(timeout=5)
        return DONE


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


def test_golden_the_loop_keeps_beating_while_the_run_is_in_flight(
    tmp_path: Path,
) -> None:
    h = HandlerLabAlarmScheduled(environ=environ(tmp_path), tick=_BlockingTick())
    event, gap = _measured(lambda: h.handle_async(tick(T0)))
    assert event is not None
    assert gap < BOUND


def test_failing_control_the_sync_handle_on_the_loop_blocks_the_heartbeat(
    tmp_path: Path,
) -> None:
    h = HandlerLabAlarmScheduled(environ=environ(tmp_path), tick=_BlockingTick())
    event, gap = _measured(lambda: h.handle(tick(T0)))
    assert event is not None
    assert gap >= BLOCK_SECONDS * 0.8


def test_error_a_second_tick_while_a_run_is_in_flight_starts_no_second_run(
    tmp_path: Path,
) -> None:
    blocking = _BlockingTick()
    h = HandlerLabAlarmScheduled(environ=environ(tmp_path), tick=blocking)

    async def two_ticks() -> list[object]:
        return await asyncio.gather(
            h.handle_async(tick(T0)),
            h.handle_async(tick(T0 + timedelta(seconds=1))),
        )

    results = asyncio.run(two_ticks())
    assert sum(result is not None for result in results) == 1
    assert blocking.calls == [ALARM_NODE]


def test_error_a_skipped_tick_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocking = _BlockingTick()
    h = HandlerLabAlarmScheduled(environ=environ(tmp_path), tick=blocking)

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
    tmp_path: Path,
) -> None:
    held = _HeldTick()
    clk = _MutableClock()
    h = HandlerLabAlarmScheduled(environ=environ(tmp_path), tick=held, clock=clk)

    async def held_run_then_a_late_tick() -> tuple[object, object]:
        first = asyncio.create_task(h.handle_async(tick(T0)))
        await asyncio.to_thread(held.started.wait, 5)
        clk.advance(400)
        second = await h.handle_async(tick(T0 + timedelta(seconds=400)))
        held.release.set()
        return await first, second

    first, second = asyncio.run(held_run_then_a_late_tick())
    assert first is not None
    assert held.calls == [ALARM_NODE]
    assert second is not None
    assert second.outcome == "failed"
    assert "in flight" in second.detail


def test_error_cancelling_the_awaiting_task_does_not_release_a_running_worker(
    tmp_path: Path,
) -> None:
    held = _HeldTick()
    h = HandlerLabAlarmScheduled(environ=environ(tmp_path), tick=held)

    async def cancelled_first_run_then_a_late_tick() -> tuple[
        object, object, list[str]
    ]:
        first = asyncio.create_task(h.handle_async(tick(T0)))
        await asyncio.to_thread(held.started.wait, 5)
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first
        second = await h.handle_async(tick(T0 + timedelta(seconds=400)))
        calls_at_second = list(held.calls)
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
    assert calls_at_second == [ALARM_NODE]
    assert third is not None
    assert held.calls == [ALARM_NODE, ALARM_NODE]


def test_seam_the_runtime_wiring_picks_handle_async() -> None:
    h = HandlerLabAlarmScheduled(environ={})
    resolved = _resolve_effective_handle_method(h)
    assert resolved is not None
    assert getattr(resolved, "__self__", None) is h
    assert (
        getattr(getattr(resolved, "__func__", None), "__name__", "") == "handle_async"
    )
    assert inspect.iscoroutinefunction(resolved)
