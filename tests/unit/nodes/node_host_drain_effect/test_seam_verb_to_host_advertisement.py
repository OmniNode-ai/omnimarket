# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24489 seam: the drain verb, the host's serve process and the capacity advertisement every launcher reads.

One in-memory bus carries both sides. A ``LabWorkHost`` serves ``example-host-a`` (it takes the drain command and advertises the
mark and the lanes it runs on every beat); the drain verb (the synchronous command line, on its own thread, over ``SyncBus.over``)
publishes the command and reads the host back; a second launcher is a second advertisement source that has never seen the verb.
The test fails when either side changes shape: the command model, the topic, the pseudo-tool of the mark, the lane tool, or the
verb's reading of them.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.lab_work.bus import LabWorkHost
from omnimarket.nodes.node_host_drain_effect import runtime_host_drain as cli
from omnimarket.nodes.node_host_drain_effect.adapters.bus_drain import (
    BusAdvertisementSource,
    SyncBus,
)
from omnimarket.nodes.node_host_drain_effect.overlay import (
    EXAMPLE_OVERLAY,
    ModelHostDrainOverlay,
)
from omnimarket.nodes.node_lab_work_unit_effect import (
    HandlerHostCapacityAdvertiseEffect,
    HandlerLabWorkUnitEffect,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    HostReading,
    draining_until_from_tools,
)

pytestmark = pytest.mark.unit

HOST = "example-host-a"
WINDOW = 1.2


class _Reader:
    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=8, load1=0.5, mem_available_bytes=16 * 2**30, tools=tuple(tools)
        )


class _Runner:
    def run(self, request: object, host_name: str) -> object:
        raise AssertionError("no unit runs here")


def _overlay(outputs: str = "0") -> ModelHostDrainOverlay:
    import sys

    import yaml

    document = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    document["profile"] = "bus"
    document["bus"] = {
        "kind": "inmemory",
        "window_seconds": WINDOW,
        "cache_seconds": 0,
        "timeout_seconds": 10,
    }
    document["drain"]["default_ttl_seconds"] = 600
    for read in document["host_reads"]:
        read["command"] = [sys.executable, "-c", f"print({outputs!r})", "{host}"]
    return ModelHostDrainOverlay.model_validate(document)


@contextmanager
def _fleet(lanes: list[str] | None) -> Iterator[tuple[SyncBus, EventBusInmemory]]:
    """A bus on a loop of its own with one serving host that beats every 0.25 s."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    state: dict[str, object] = {}

    async def serve() -> None:
        bus = EventBusInmemory(environment="local", group="seam-test")
        await bus.start()
        host = LabWorkHost(
            bus,
            HandlerLabWorkUnitEffect(HOST, _Runner()),  # type: ignore[arg-type]
            HandlerHostCapacityAdvertiseEffect(_Reader()),
            lane_reader=(lambda: lanes),
        )
        await host.start(advertise=False)
        state["bus"] = bus
        stop = asyncio.Event()
        state["stop"] = stop
        ready.set()

        async def beat() -> None:
            while not stop.is_set():
                await host.advertise_once()
                await asyncio.sleep(0.25)

        task = asyncio.create_task(beat())
        await stop.wait()
        task.cancel()
        await host.stop()
        await bus.close()

    thread = threading.Thread(
        target=loop.run_until_complete, args=(serve(),), daemon=True
    )
    thread.start()
    assert ready.wait(10)
    bus = state["bus"]
    assert isinstance(bus, EventBusInmemory)
    sync = SyncBus.over(loop, bus, timeout_seconds=10)
    try:
        yield sync, bus
    finally:
        stop = state["stop"]
        assert isinstance(stop, asyncio.Event)
        loop.call_soon_threadsafe(stop.set)
        thread.join(timeout=10)


def _run(
    argv: list[str],
    sync: SyncBus,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    overlay: ModelHostDrainOverlay,
) -> tuple[int, str]:
    environ = {overlay.emission.home_env: str(tmp_path)}
    code = cli.run(argv, overlay=overlay, environ=environ, sync_bus=sync)
    return code, capsys.readouterr().out.strip()


def test_golden_a_drained_host_with_a_lane_running_reads_not_safe_and_names_the_lane(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = _overlay()
    with _fleet(["drain-fix-6809"]) as (sync, _bus):
        code, line = _run(
            ["drain", "--host", HOST, "--line"], sync, tmp_path, capsys, overlay
        )
        assert code == 0
        assert line.startswith(f"{HOST} draining until ")
        assert "[drain-fix-6809]" in line
        assert line.endswith(": NOT SAFE to power down")


def test_a_second_launcher_that_never_saw_the_verb_reads_the_same_mark(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = _overlay()
    with _fleet([]) as (sync, bus):
        code, _ = _run(
            ["drain", "--host", HOST, "--line"], sync, tmp_path, capsys, overlay
        )
        assert code == 0
        second = BusAdvertisementSource(
            SyncBus.over(sync._loop, bus, timeout_seconds=10),
            window_seconds=WINDOW,
            cache_seconds=0,
        )
        ads = [ad for ad in second.advertisements() if ad.host_name == HOST]
        assert ads
        mark = draining_until_from_tools(ads[-1].tools)
        assert mark is not None
        assert mark > datetime.now(UTC)


def test_an_idle_host_with_every_read_zero_and_a_mark_on_the_bus_reads_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = _overlay()
    with _fleet([]) as (sync, _bus):
        code, line = _run(
            ["drain", "--host", HOST, "--line"], sync, tmp_path, capsys, overlay
        )
        assert code == 0
        assert line.endswith(": SAFE to power down")
        assert "0 lane(s) running" in line


def test_failing_control_a_host_whose_lanes_were_not_read_is_never_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same idle host, but its lane read failed (lane_reader answers None): the line must not read SAFE."""
    overlay = _overlay()
    with _fleet(None) as (sync, _bus):
        code, line = _run(
            ["drain", "--host", HOST, "--line"], sync, tmp_path, capsys, overlay
        )
        assert code == 0
        assert line.endswith(": NOT SAFE to power down")
        assert "unreadable" in line


def test_a_host_read_that_prints_no_integer_is_never_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = _overlay("not-a-number")
    with _fleet([]) as (sync, _bus):
        code, line = _run(
            ["drain", "--host", HOST, "--line"], sync, tmp_path, capsys, overlay
        )
        assert code == 0
        assert line.endswith(": NOT SAFE to power down")
        assert "model_servers=unreadable" in line


def test_undrain_clears_the_mark_the_next_advertisement_carries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = _overlay()
    with _fleet([]) as (sync, _bus):
        assert _run(["drain", "--host", HOST], sync, tmp_path, capsys, overlay)[0] == 0
        code, out = _run(["undrain", "--host", HOST], sync, tmp_path, capsys, overlay)
        assert code == 0
        assert json.loads(out)["draining"] is False
        code, out = _run(
            ["check-start", "--host", HOST], sync, tmp_path, capsys, overlay
        )
        assert json.loads(out)["admitted"] is True


def test_check_start_refuses_a_draining_host_across_two_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = _overlay()
    with _fleet([]) as (sync, _bus):
        assert _run(["drain", "--host", HOST], sync, tmp_path, capsys, overlay)[0] == 0
        code, out = _run(
            ["check-start", "--host", HOST], sync, tmp_path, capsys, overlay
        )
        assert code == 1
        assert json.loads(out)["outcome"] == "start_refused"
        code, out = _run(["draining"], sync, tmp_path, capsys, overlay)
        assert out.split() == [HOST]
