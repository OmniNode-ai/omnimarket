# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21811 -- a lab-work host keeps advertising through a failed publish, and its
serve process exits when the advertiser has been silent past the contract's bound.

2026-10-31: the CI bus broker was down from about 10:05Z to 13:26Z. On every pool host
one failed capacity publish ended the advertiser task, whose exception nothing read,
while the command consumer rejoined and the process stayed up. The capacity topic held
no record newer than 11:12Z, ``lab-run --via bus`` read zero advertisements and exited
75 for the rest of the day. The clock is injected, so the tests advance it instead of
sleeping.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models import ModelEventHeaders, ModelPublishReceipt

from omnimarket.lab_work import cli
from omnimarket.lab_work.bus import LabWorkHost, load_lab_work_topics
from omnimarket.nodes.node_lab_work_unit_effect import (
    EnumLabWorkUnitStatus,
    HandlerHostCapacityAdvertiseEffect,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.handlers import (
    HostAdvertiser,
)
from omnimarket.nodes.node_lab_work_unit_effect.handlers import (
    handler_host_capacity_advertise_effect as advertise,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import HostReading

pytestmark = pytest.mark.unit

HOST = "h051"
CADENCE_S = 1
BOUND_S = advertise.silent_exit_seconds()


class _Reader:
    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=8, load1=1.0, mem_available_bytes=16 * 1024**3, tools=("git",)
        )


class _Work:
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


class _BrokerDownBus(EventBusInmemory):
    """A bus whose capacity publish raises while ``down`` is not zero (a positive count falls with each failure; -1 never recovers)."""

    def __init__(self) -> None:
        super().__init__(environment="local", group="lab-work-advertiser-test")
        self.down = 0
        self.capacity_topic = load_lab_work_topics().capacity

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: ModelEventHeaders | None = None,
    ) -> ModelPublishReceipt:
        if topic == self.capacity_topic and self.down != 0:
            if self.down > 0:
                self.down -= 1
            raise ConnectionError("broker not available")
        return await super().publish(topic, key, value, headers)


async def _until(predicate: Callable[[], bool], what: str, tries: int = 600) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def _with_host(
    scenario: Callable[[LabWorkHost, _BrokerDownBus, _Clock], Awaitable[None]],
    *,
    advertise_capacity: bool = True,
) -> None:
    bus = _BrokerDownBus()
    await bus.start()
    clock = _Clock()
    topics = load_lab_work_topics().model_copy(update={"cadence_seconds": CADENCE_S})
    host = LabWorkHost(
        bus,
        _Work(),
        HandlerHostCapacityAdvertiseEffect(_Reader()),
        topics=topics,
        check_seconds=0.01,
    )
    host.advertiser = HostAdvertiser(
        HOST, clock
    )  # the advertiser's own clock, so the test ages only it
    await host.start(advertise=advertise_capacity)
    try:
        await scenario(host, bus, clock)
    finally:
        await host.stop()
        await bus.close()


def test_publish_failure_does_not_end_the_advertiser(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario(host: LabWorkHost, bus: _BrokerDownBus, clock: _Clock) -> None:
        bus.down = 2  # the first two beats find the broker down
        await _until(lambda: host.advertiser.failures >= 2, "two failed publishes")
        assert host.advertiser_alive, "one failed publish ended the advertiser task"
        await _until(lambda: host.advertised >= 1, "an advertisement after recovery")
        assert host.advertiser_alive

    with caplog.at_level(logging.INFO):
        asyncio.run(_with_host(scenario))
    assert any(
        "capacity publish failed" in r.getMessage() and r.levelno >= logging.ERROR
        for r in caplog.records
    )


def test_silent_exit_serve_leaves_non_zero_once_the_advertiser_is_silent_past_the_bound(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exits: list[int] = []
    monkeypatch.setattr(cli, "_hard_exit", exits.append)

    async def scenario(host: LabWorkHost, bus: _BrokerDownBus, clock: _Clock) -> None:
        bus.down = -1  # the broker never comes back
        serving = asyncio.create_task(cli.serve_until_stopped(host, asyncio.Event(), 0))
        await _until(lambda: host.advertiser.failures >= 1, "a failed publish")
        clock.now += BOUND_S - 5
        before = host.checks
        await _until(lambda: host.checks >= before + 3, "the watcher to pass")
        assert not host.stuck.is_set(), "silent before the bound"
        assert not exits
        clock.now += 10
        await _until(host.stuck.is_set, "the silent verdict")
        assert host.stuck_unit == "<capacity advertiser>"
        assert await asyncio.wait_for(serving, 5) == cli.EXIT_STUCK_WORKER
        assert exits == [cli.EXIT_STUCK_WORKER]

    with caplog.at_level(logging.INFO):
        asyncio.run(_with_host(scenario))
    assert any(
        r.levelno >= logging.CRITICAL and "no capacity advertisement" in r.getMessage()
        for r in caplog.records
    )


def test_silent_exit_a_beating_host_is_never_silent() -> None:
    async def scenario(host: LabWorkHost, bus: _BrokerDownBus, clock: _Clock) -> None:
        for _ in range(3):
            published = host.advertised
            clock.now += (
                BOUND_S - 20
            )  # three of these pass the bound if a beat is skipped
            await _until(
                lambda before=published: host.advertised > before,
                "the next advertisement",
            )
            await _until(lambda: host.checks >= 2, "the watcher to pass")
        assert not host.stuck.is_set()

    asyncio.run(_with_host(scenario))


def test_silent_exit_a_host_started_without_an_advertiser_is_not_judged() -> None:
    async def scenario(host: LabWorkHost, bus: _BrokerDownBus, clock: _Clock) -> None:
        clock.now += 10 * BOUND_S
        before = host.checks
        await _until(lambda: host.checks >= before + 3, "the watcher to pass")
        assert not host.stuck.is_set()

    asyncio.run(_with_host(scenario, advertise_capacity=False))


def test_bound_from_contract_is_read_from_the_node_contract_and_no_module_holds_it() -> (
    None
):
    contract = yaml.safe_load(Path(advertise.CONTRACT_PATH).read_text())
    declared = contract["capacity"]["silent_exit_seconds"]
    assert declared == BOUND_S
    assert declared > 2 * contract["capacity"]["cadence_seconds"], (
        "a bound under two cadences would exit a host for one slow beat"
    )
    for path in (
        Path(advertise.__file__),
        Path(cli.__file__),
        Path(cli.__file__).with_name("bus.py"),
    ):
        assert f"= {declared}" not in path.read_text()
        assert f"> {declared}" not in path.read_text()
