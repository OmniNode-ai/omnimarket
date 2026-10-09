# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20799 -- a lab-work worker that holds a unit past its limit is visible
and does not stay behind a live advertiser: it logs progress while it holds the
unit and the serve process exits non-zero after two limits."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.lab_work.bus import LabWorkCaller, LabWorkHost
from omnimarket.nodes.node_lab_work_unit_effect import (
    EnumLabWorkUnitStatus,
    HandlerHostCapacityAdvertiseEffect,
    HandlerLabWorkUnitEffect,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    HostReading,
)

pytestmark = pytest.mark.unit

SHA = "4c3985a451847571e2cebd06a6edf1940069946e"


class _Reader:
    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=8, load1=0.5, mem_available_bytes=32 * 1024**3, tools=("git", "uv")
        )


class _HangingRunner:
    """A runner that never answers until released: the stuck-thread case."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()

    def run(
        self, request: ModelLabWorkUnitRequest, host_name: str
    ) -> ModelLabWorkUnitReceipt:
        self.entered.set()
        self.release.wait(timeout=60)
        return _receipt(request, host_name)


class _SlowRunner:
    """A runner that answers inside its limit, but after a progress interval."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def run(
        self, request: ModelLabWorkUnitRequest, host_name: str
    ) -> ModelLabWorkUnitReceipt:
        threading.Event().wait(self.seconds)
        return _receipt(request, host_name)


def _receipt(
    request: ModelLabWorkUnitRequest, host_name: str
) -> ModelLabWorkUnitReceipt:
    return ModelLabWorkUnitReceipt(
        work_unit_id=request.work_unit_id,
        target_host=request.target_host,
        host=host_name,
        repo=request.repo,
        commit_sha=request.commit_sha,
        status=EnumLabWorkUnitStatus.COMPLETED,
        exit_code=0,
    )


def _request(**overrides: object) -> ModelLabWorkUnitRequest:
    fields: dict[str, object] = {
        "work_unit_id": "lw-stuck0123456789",
        "target_host": "h202",
        "repo": "OmniNode-ai/omnimarket",
        "commit_sha": SHA,
        "argv": ["uv", "run", "pytest", "-q"],
        "kind": "test",
        "timeout_seconds": 1,
        "lane": "test-lane",
    }
    fields.update(overrides)
    return ModelLabWorkUnitRequest.model_validate(fields)


def test_lab_work_serve_stuck_worker_exits_flags_stuck_while_advertiser_lives(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = _HangingRunner()
    caplog.set_level(logging.INFO, logger="omnimarket.lab_work.bus")

    async def scenario() -> tuple[LabWorkHost, int, ModelLabWorkUnitReceipt]:
        bus = EventBusInmemory(environment="local", group="lab-work-stuck-test")
        await bus.start()
        host = LabWorkHost(
            bus,
            HandlerLabWorkUnitEffect("h202", runner),
            HandlerHostCapacityAdvertiseEffect(_Reader()),
            progress_seconds=0.3,
        )
        caller = LabWorkCaller(bus, wait_slack_seconds=10)
        await host.start()
        await caller.start()
        try:
            receipt = await caller.dispatch(_request(timeout_seconds=1))
            assert host.stuck.is_set()
            before = host.advertised
            await host.advertise_once()
            return host, host.advertised - before, receipt
        finally:
            runner.release.set()
            await caller.stop()
            await host.stop()
            await bus.close()

    host, advertised_after, receipt = asyncio.run(scenario())

    assert runner.entered.is_set()
    assert advertised_after == 1, "the advertiser must still be alive after the stall"
    # the caller is answered on the failure terminal instead of waiting out its slack
    assert receipt.status is EnumLabWorkUnitStatus.INFRA_ERROR
    assert "held" in receipt.detail
    assert "lw-stuck0123456789" in host.stuck_reason
    progress = [r for r in caplog.records if "holding unit" in r.getMessage()]
    assert len(progress) >= 2, [r.getMessage() for r in caplog.records]
    assert any("past its" in r.getMessage() for r in progress)


def test_lab_work_serve_stuck_worker_exits_not_for_a_unit_that_answers_in_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="omnimarket.lab_work.bus")

    async def scenario() -> tuple[LabWorkHost, ModelLabWorkUnitReceipt]:
        bus = EventBusInmemory(environment="local", group="lab-work-slow-test")
        await bus.start()
        host = LabWorkHost(
            bus,
            HandlerLabWorkUnitEffect("h202", _SlowRunner(0.7)),
            HandlerHostCapacityAdvertiseEffect(_Reader()),
            progress_seconds=0.2,
        )
        caller = LabWorkCaller(bus, wait_slack_seconds=10)
        await host.start(advertise=False)
        await caller.start()
        try:
            receipt = await caller.dispatch(_request(timeout_seconds=30))
            return host, receipt
        finally:
            await caller.stop()
            await host.stop()
            await bus.close()

    host, receipt = asyncio.run(scenario())

    assert receipt.status is EnumLabWorkUnitStatus.COMPLETED
    assert not host.stuck.is_set()
    assert [r for r in caplog.records if "holding unit" in r.getMessage()], (
        "a unit held across a progress interval logs a progress line"
    )


def test_lab_work_serve_stuck_worker_exits_nonzero_from_the_serve_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from omnimarket.lab_work import cli
    from omnimarket.nodes.node_lab_work_unit_effect import protocols

    runner = _HangingRunner()
    monkeypatch.setattr(protocols, "LocalShellLabWorkExecutor", lambda _root: runner)
    answers: list[ModelLabWorkUnitReceipt] = []

    def exit_hard(code: int) -> None:
        # the real one is os._exit: a worker thread that never returns would
        # otherwise hold the interpreter open after the serve loop is done
        runner.release.set()
        raise SystemExit(code)

    monkeypatch.setattr(cli, "_exit_hard", exit_hard)

    @asynccontextmanager
    async def open_pool(**kwargs: object) -> AsyncIterator[EventBusInmemory]:
        bus = EventBusInmemory(environment="local", group="lab-work-serve-stuck-test")
        await bus.start()

        async def send_one_unit() -> None:
            caller = LabWorkCaller(bus, wait_slack_seconds=10)
            await caller.start()
            await asyncio.sleep(0.3)
            answers.append(await caller.dispatch(_request(timeout_seconds=1)))
            await caller.stop()

        task = asyncio.create_task(send_one_unit())
        try:
            yield bus
        finally:
            task.cancel()
            await bus.close()

    monkeypatch.setattr(cli, "open_lab_run_bus", open_pool)

    result = CliRunner().invoke(
        cli.lab_work_group, ["serve", "--bus", "inmemory", "--host-name", "h202"]
    )

    assert result.exit_code == cli.EXIT_STUCK_WORKER, result.output
    assert cli.EXIT_STUCK_WORKER != 0
    assert runner.entered.is_set()
