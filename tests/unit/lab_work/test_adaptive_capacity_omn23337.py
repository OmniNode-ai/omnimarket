# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23337 -- the capacity advertisement records its inputs and carries the computed slot cap."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from omnimarket.handlers.host_capacity_overlay import load_host_capacity_limits
from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.model_lab_fill_fire_headroom import (
    ModelLabFillFireHeadroomRequest,
)
from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_fire_plan import (
    HandlerLabFillFirePlan,
)
from omnimarket.nodes.node_lab_work_unit_effect.handlers.handler_host_capacity_advertise_effect import (
    HandlerHostCapacityAdvertiseEffect,
)
from omnimarket.nodes.node_lab_work_unit_effect.models import (
    ModelHostCapacityProbeRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_host_reader import (
    HostReading,
    LocalHostReader,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("host_capacity_overlay")]
GB = 1024**3
NOW = datetime(2026, 11, 9, 14, 0, tzinfo=UTC)


class _Reader:
    def __init__(self, reading: HostReading) -> None:
        self._reading = reading

    def read(self, tools: list[str]) -> HostReading:
        return self._reading


def _reading(
    *,
    cores: int = 32,
    load1: float = 2.0,
    total: float | None = 96.0,
    avail: float = 80.0,
    swap_in: float | None = 0.0,
    swap_out: float | None = 0.0,
) -> HostReading:
    return HostReading(
        cores=cores,
        load1=load1,
        mem_available_bytes=int(avail * GB),
        tools=("git",),
        mem_total_bytes=None if total is None else int(total * GB),
        swap_in_kb_s=swap_in,
        swap_out_kb_s=swap_out,
    )


def _advertise(
    reading: HostReading, *, running: int = 0, workers: int = 32, **kw: Any
) -> Any:
    handler = HandlerHostCapacityAdvertiseEffect(
        reader=_Reader(reading), now=lambda: NOW, **kw
    )
    return handler.handle(
        ModelHostCapacityProbeRequest(
            host_name="h202", running_units=running, max_units=workers
        )
    )


def test_adaptive_slot_cap_is_advertised_with_its_inputs() -> None:
    ad = _advertise(_reading(load1=2.0, avail=80.0), running=3)
    lim = load_host_capacity_limits()
    cpu_slots = int((lim.slot_load_per_core * 32 - 2.0) // lim.cores_per_slot)
    assert ad.max_units == min(
        16, 3 + cpu_slots
    )  # the overlay's ceiling holds the cap at 16 on 32 cores
    assert ad.capacity_refusal == ""
    for cell in (
        "cap=",
        "running=3",
        "cores=32",
        "load1=2.00",
        "mem_avail_gb=80.0/96.0",
        "workers=32",
    ):
        assert cell in ad.slot_cap_record
    assert ad.mem_total_bytes == 96 * GB
    assert ad.swap_in_kb_s == 0.0


def test_adaptive_slot_cap_the_advertised_cap_follows_measured_memory() -> None:
    plenty = _advertise(_reading(avail=80.0))
    tight = _advertise(_reading(avail=14.0))
    assert tight.max_units < plenty.max_units
    assert "binding=memory" in tight.slot_cap_record


def test_adaptive_slot_cap_never_exceeds_the_worker_pool() -> None:
    assert _advertise(_reading(avail=80.0), workers=3).max_units == 3


def test_adaptive_slot_cap_a_refused_host_advertises_its_refusal_and_a_cap_of_at_least_one() -> (
    None
):
    ad = _advertise(_reading(load1=34.5, avail=46.0))
    assert "1.08 per core" in ad.capacity_refusal
    assert ad.max_units >= 1


def test_adaptive_slot_cap_swapping_host_is_refused() -> None:
    ad = _advertise(_reading(swap_in=50000.0))
    assert "swap" in ad.capacity_refusal
    assert "binding=swap" in ad.slot_cap_record


def test_error_an_absent_overlay_advertises_the_worker_pool_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def absent() -> Any:
        raise NodeOverlayNotFoundError("host_capacity: not configured")

    ad = _advertise(_reading(), workers=6, limits=absent)
    assert ad.max_units == 6
    assert ad.capacity_refusal == ""
    assert "overlay unavailable" in ad.slot_cap_record


def test_error_a_reading_with_no_total_memory_is_not_judged() -> None:
    ad = _advertise(_reading(total=None), workers=6)
    assert ad.max_units == 6
    assert "total memory unread" in ad.slot_cap_record


def test_the_local_reader_reads_total_memory_and_a_swap_rate_from_the_second_read() -> (
    None
):
    clock = iter([100.0, 110.0])
    reader = LocalHostReader(now=lambda: next(clock))
    first = reader.read([])
    second = reader.read([])
    assert first.mem_total_bytes
    assert first.mem_total_bytes >= first.mem_available_bytes
    assert first.swap_in_kb_s is None, "no earlier counters, no rate"
    assert second.swap_in_kb_s is not None
    assert second.swap_in_kb_s >= 0.0


def test_a_refused_hosts_free_slots_are_zero_in_the_lab_fill_plan() -> None:
    seen: list[Any] = []

    class _Headroom:
        def handle(self, request: Any) -> str:
            seen.append(request)
            return "plan"

    refused = _advertise(_reading(load1=34.5, avail=46.0), workers=8)
    open_ = _advertise(_reading(load1=2.0, avail=80.0), workers=8)
    open_ = open_.model_copy(update={"host_name": "h201"})
    request = ModelLabFillFireHeadroomRequest.model_construct(
        fire=SimpleNamespace(fire_id="f1"),
        observed_at=NOW,
        readings=(refused, open_),
        max_lanes=8,
    )
    HandlerLabFillFirePlan(headroom=cast(Any, _Headroom())).handle(request)
    slots = {p.name: p.runner_slots for p in seen[0].probes}
    assert slots["h202"] == 0
    assert slots["h201"] > 0
