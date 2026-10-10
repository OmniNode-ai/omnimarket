# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20799 -- a worker that holds a unit it never answers logs progress and
makes ``onex lab-work serve`` exit non-zero after two limits, while the
advertiser behind it is still alive.

The clock is injected, so the tests advance it instead of sleeping.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.lab_work import cli
from omnimarket.lab_work.bus import LabWorkCaller, LabWorkHost, ProtocolWorkHandler
from omnimarket.nodes.node_lab_work_unit_effect import (
    EnumLabWorkUnitStatus,
    HandlerHostCapacityAdvertiseEffect,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import HostReading

pytestmark = pytest.mark.unit

SHA = "4c3985a451847571e2cebd06a6edf1940069946e"
HOST = "h202"


class _Reader:
    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=8, load1=1.0, mem_available_bytes=16 * 1024**3, tools=("git",)
        )


class _HangingWork:
    """A work handler whose unit never returns: the wedged worker."""

    host_name = HOST

    async def handle(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _QuickWork:
    host_name = HOST

    async def handle(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        return ModelLabWorkUnitReceipt(
            work_unit_id=request.work_unit_id,
            target_host=request.target_host,
            host=HOST,
            repo=request.repo,
            commit_sha=request.commit_sha,
            status=EnumLabWorkUnitStatus.COMPLETED,
            exit_code=0,
        )


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _request(
    timeout_seconds: int, unit: str = "lw-0123456789abcdef"
) -> ModelLabWorkUnitRequest:
    return ModelLabWorkUnitRequest.model_validate(
        {
            "work_unit_id": unit,
            "target_host": HOST,
            "repo": "OmniNode-ai/omnimarket",
            "commit_sha": SHA,
            "argv": ["uv", "run", "pytest", "-q"],
            "kind": "test",
            "timeout_seconds": timeout_seconds,
            "lane": "stuck-lane",
        }
    )


async def _until(predicate: Callable[[], bool], what: str) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def _tick(host: LabWorkHost) -> None:
    """Let the watcher run a few passes against the current fake time."""
    before = host.checks
    await _until(lambda: host.checks >= before + 2, "two progress checks")


async def _with_host(
    work: ProtocolWorkHandler,
    scenario: Callable[[LabWorkHost, LabWorkCaller, _Clock], Awaitable[None]],
) -> None:
    bus = EventBusInmemory(environment="local", group="lab-work-stuck-test")
    await bus.start()
    clock = _Clock()
    host = LabWorkHost(
        bus,
        work,
        HandlerHostCapacityAdvertiseEffect(_Reader()),
        progress_seconds=300.0,
        check_seconds=0.01,
        clock=clock,
    )
    caller = LabWorkCaller(bus, wait_slack_seconds=1)
    await host.start()
    await caller.start()
    try:
        await scenario(host, caller, clock)
    finally:
        await caller.stop()
        await host.stop()
        await bus.close()


def test_lab_work_serve_stuck_worker_exits_after_two_limits_while_advertiser_alive(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario(host: LabWorkHost, caller: LabWorkCaller, clock: _Clock) -> None:
        sender = asyncio.create_task(caller.dispatch(_request(timeout_seconds=100)))
        await _until(lambda: host.running == 1, "the worker to hold the unit")
        await _until(lambda: host.advertised >= 1, "the advertiser to beat")

        clock.now += 60
        await _tick(host)
        assert not host.stuck.is_set()
        assert "past its limit" not in caplog.text

        clock.now += 50  # 110s held, limit 100s
        await _tick(host)
        assert not host.stuck.is_set()
        assert "lw-0123456789abcdef" in caplog.text
        assert "past its limit" in caplog.text

        clock.now += 100  # 210s held, over two limits
        await _until(host.stuck.is_set, "the stuck verdict")
        assert host.stuck_unit == "lw-0123456789abcdef"
        assert host.advertiser_alive, (
            "the advertiser is still beating behind the stuck worker"
        )
        sender.cancel()

    with caplog.at_level(logging.INFO, logger="omnimarket.lab_work.bus"):
        asyncio.run(_with_host(_HangingWork(), scenario))
    assert any(
        r.levelno >= logging.CRITICAL and "stuck" in r.getMessage()
        for r in caplog.records
    )


def test_lab_work_serve_stuck_worker_progress_line_every_five_minutes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario(host: LabWorkHost, caller: LabWorkCaller, clock: _Clock) -> None:
        sender = asyncio.create_task(caller.dispatch(_request(timeout_seconds=3600)))
        await _until(lambda: host.running == 1, "the worker to hold the unit")

        def lines() -> int:
            return sum("holding unit" in r.getMessage() for r in caplog.records)

        clock.now += 299
        await _tick(host)
        assert lines() == 0
        clock.now += 2  # 301s
        await _until(lambda: lines() == 1, "the first progress line")
        clock.now += 299  # 600s: still inside the second window
        await _tick(host)
        assert lines() == 1
        clock.now += 2  # 602s
        await _until(lambda: lines() == 2, "the second progress line")
        assert not host.stuck.is_set()
        sender.cancel()

    with caplog.at_level(logging.INFO, logger="omnimarket.lab_work.bus"):
        asyncio.run(_with_host(_HangingWork(), scenario))


def test_lab_work_serve_stuck_worker_answered_unit_and_idle_host_are_not_stuck(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario(host: LabWorkHost, caller: LabWorkCaller, clock: _Clock) -> None:
        receipt = await caller.dispatch(_request(timeout_seconds=100))
        assert receipt.status is EnumLabWorkUnitStatus.COMPLETED
        assert host.running == 0
        clock.now += 10_000  # long after any limit, with nothing held
        await _tick(host)
        assert not host.stuck.is_set()
        assert "holding unit" not in caplog.text

    with caplog.at_level(logging.INFO, logger="omnimarket.lab_work.bus"):
        asyncio.run(_with_host(_QuickWork(), scenario))


def test_lab_work_serve_stuck_worker_exits_serve_loop_non_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exits: list[int] = []
    monkeypatch.setattr(cli, "_hard_exit", exits.append)

    async def scenario(host: LabWorkHost, caller: LabWorkCaller, clock: _Clock) -> None:
        sender = asyncio.create_task(caller.dispatch(_request(timeout_seconds=100)))
        await _until(lambda: host.running == 1, "the worker to hold the unit")
        serving = asyncio.create_task(cli.serve_until_stopped(host, asyncio.Event(), 0))
        clock.now += 250
        code = await asyncio.wait_for(serving, timeout=5)
        assert code == cli.EXIT_STUCK_WORKER != 0
        sender.cancel()

    asyncio.run(_with_host(_HangingWork(), scenario))
    assert exits == [cli.EXIT_STUCK_WORKER]
