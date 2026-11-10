# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Admission and slot cap follow a host's measured capacity (OMN-23337).

The readings are the lab's of 2026-11-09: h201 32 cores / 96 GB at load 34.5 to 100, h202 32 cores / 63 GB at load 5.7,
h101 12 cores / 32 GB at load 7.9, h105 10 cores / 32 GB, a many-core server at load 12.7 to 16.5. The limits are the overlay's
starting values, loaded through the overlay reader like production; no limit is written in these tests except where one is the
injected break a test names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.host_capacity_overlay import load_host_capacity_limits
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_host_capacity_compute import HandlerHostCapacity
from omnimarket.nodes.node_host_capacity_compute.models import (
    ModelHostCapacityLimits,
    ModelHostCapacityReading,
    ModelHostCapacityRequest,
    ModelHostCapacityVerdict,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("host_capacity_overlay")]

#: The bar the focused test run held before OMN-23337, as an absolute one-minute load.
OLD_ABSOLUTE_LOAD_BAR = 8.0


def limits() -> ModelHostCapacityLimits:
    return load_host_capacity_limits()


def judge(
    *,
    host: str = "h202",
    cores: int = 32,
    total: float = 64.0,
    avail: float = 48.0,
    load: float = 2.0,
    running: int = 0,
    swap_in: float | None = 0.0,
    swap_out: float | None = 0.0,
    lim: ModelHostCapacityLimits | None = None,
) -> ModelHostCapacityVerdict:
    return HandlerHostCapacity().handle(
        ModelHostCapacityRequest(
            reading=ModelHostCapacityReading(
                host=host,
                logical_cores=cores,
                mem_total_gb=total,
                mem_avail_gb=avail,
                load_one_minute=load,
                running_lanes=running,
                swap_in_kb_s=swap_in,
                swap_out_kb_s=swap_out,
            ),
            limits=lim or limits(),
        )
    )


def test_golden_a_quiet_host_is_admitted_with_a_cap_and_its_inputs() -> None:
    v = judge(load=2.0, running=3)
    assert v.admitted
    assert v.refusals == ()
    assert v.free_slots >= 1
    assert v.slot_cap == min(
        v.slot_ceiling, 3 + min(v.cpu_headroom_slots, v.memory_headroom_slots)
    )
    # the verdict carries the reading and the limits it came from, and prints them
    assert v.inputs.reading.logical_cores == 32
    assert v.inputs.limits == limits()
    line = v.record()
    for cell in (
        "cap=",
        "free=",
        "running=3",
        "cores=32",
        "load1=2.00",
        "mem_avail_gb=48.0/64.0",
        "swap_kb_s_in/out=0/0",
    ):
        assert cell in line


def test_admission_per_core_a_large_host_at_moderate_load_per_core_is_admitted() -> (
    None
):
    # h051 read load 12.7 to 16.5 and h053 14.2: over the old absolute bar, a fifth of a core's load each on a many-core host.
    for cores, load in ((96, 12.7), (96, 16.5), (64, 14.2)):
        assert load > OLD_ABSOLUTE_LOAD_BAR
        v = judge(host="big", cores=cores, total=256.0, avail=200.0, load=load)
        assert v.admitted, (cores, load, v.refusals)


def test_admission_per_core_the_smallest_hosts_are_no_looser_than_the_old_bar() -> None:
    # h105 (10 cores), h101 (12 cores), h201 and h202 (32 cores): the load the overlay admits to is at most the old bar.
    for cores in (10, 12, 32):
        edge = limits().max_load_per_core * cores
        assert edge <= OLD_ABSOLUTE_LOAD_BAR, (cores, edge)
        over = judge(host="small", cores=cores, load=OLD_ABSOLUTE_LOAD_BAR + 0.5)
        assert not over.admitted
        assert "per core" in over.refusals[0]


def test_admission_per_core_the_same_load_is_refused_on_a_small_host_and_admitted_on_a_large_one() -> (
    None
):
    small = judge(host="h201", cores=32, load=20.0)
    large = judge(host="big", cores=128, total=512.0, avail=400.0, load=20.0)
    assert not small.admitted
    assert large.admitted


def test_admission_per_core_h201_at_load_34_is_refused_by_load_per_core_not_by_an_absolute_number() -> (
    None
):
    v = judge(host="h201", cores=32, total=96.0, avail=46.0, load=34.5)
    assert not v.admitted
    assert any(
        f"{34.5 / 32:.2f} per core > {limits().max_load_per_core}" in r
        for r in v.refusals
    )


def test_admission_per_core_free_memory_is_a_share_of_total() -> None:
    lim = limits()
    total = 96.0
    just_under = total * lim.min_free_memory_ratio - 0.5
    v = judge(total=total, avail=just_under, load=0.0)
    assert not v.admitted
    assert any("free memory" in r for r in v.refusals)
    # the same free gigabytes on a host a third the size are a larger share and pass the bar
    ok = judge(total=total / 3, avail=just_under + 8.0, load=0.0)
    assert ok.admitted


def test_admission_per_core_swap_activity_refuses_and_leaves_no_free_slot() -> None:
    lim = limits()
    v = judge(load=0.0, swap_in=lim.max_swap_in_kb_s + 1, swap_out=0.0)
    assert not v.admitted
    assert v.free_slots == 0
    assert v.binding == "swap"
    v2 = judge(load=0.0, swap_in=lim.max_swap_in_kb_s / 2, swap_out=lim.max_swap_kb_s)
    assert not v2.admitted
    assert judge(load=0.0, swap_in=None, swap_out=None).admitted, (
        "an unread swap sample is never judged"
    )


def test_adaptive_slot_cap_follows_measured_memory() -> None:
    plenty = judge(cores=32, total=96.0, avail=80.0, load=0.0)
    tight = judge(cores=32, total=96.0, avail=14.0, load=0.0)
    assert tight.binding == "memory"
    assert plenty.binding in {"cpu", "ceiling"}
    assert tight.slot_cap < plenty.slot_cap, (
        "a cap that ignores measured memory fails here"
    )
    assert tight.memory_headroom_slots < tight.cpu_headroom_slots


def test_adaptive_slot_cap_follows_measured_load_and_running_lanes() -> None:
    idle = judge(load=0.0, running=0)
    busy = judge(load=limits().max_load_per_core * 32 - 4.0, running=0)
    assert busy.cpu_headroom_slots < idle.cpu_headroom_slots
    assert busy.slot_cap < idle.slot_cap
    # lanes already running count in the cap, and the headroom is what is left beyond them
    carrying = judge(load=limits().max_load_per_core * 32 - 4.0, running=5)
    assert carrying.slot_cap == min(carrying.slot_ceiling, 5 + carrying.free_slots)


def test_adaptive_slot_cap_is_held_between_the_overlay_floor_and_ceiling() -> None:
    lim = limits()
    high = judge(cores=32, total=512.0, avail=500.0, load=0.0, lim=lim)
    assert high.slot_cap == high.slot_ceiling == int(lim.slot_ceiling_per_core * 32)
    refused = judge(cores=32, load=lim.max_load_per_core * 32 + 5, lim=lim)
    assert not refused.admitted
    assert refused.free_slots == 0
    assert refused.slot_cap == int(
        (lim.slot_load_per_core * 32 - refused.inputs.reading.load_one_minute)
        // lim.cores_per_slot
    )
    swamped = judge(cores=32, load=lim.slot_load_per_core * 32 + 50, lim=lim)
    assert swamped.slot_cap == lim.slot_floor


def test_adaptive_slot_cap_a_host_named_in_the_overlay_has_its_own_bounds() -> None:
    lim = ModelHostCapacityLimits.model_validate(
        {
            **limits().model_dump(),
            "hosts": {"h201": {"slot_floor": 2, "slot_ceiling": 6}},
        }
    )
    v = judge(host="h201", cores=32, total=512.0, avail=500.0, load=0.0, lim=lim)
    assert (v.slot_floor, v.slot_ceiling, v.slot_cap) == (2, 6, 6)


def test_adaptive_slot_cap_free_slots_and_admission_are_one_answer_on_every_reading() -> (
    None
):
    # The floor shows free_slots; admission is `admitted`. They must agree on every reading, whatever binds.
    lim = limits()
    for cores in (10, 32, 96):
        for load in (0.0, 1.0, 2.4, 2.6, 7.9, 8.1, 20.0, 80.0):
            for avail in (1.0, 9.0, 20.0, 60.0):
                for running in (0, 3, 40):
                    for swap in (0.0, lim.max_swap_in_kb_s + 1):
                        v = judge(
                            cores=cores,
                            total=64.0,
                            avail=avail,
                            load=load,
                            running=running,
                            swap_in=swap,
                            lim=lim,
                        )
                        assert v.admitted == (v.free_slots >= 1), v.record()
                        assert v.admitted == (not v.refusals)
                        assert (
                            v.slot_floor
                            <= v.slot_cap
                            <= max(v.slot_floor, v.slot_ceiling)
                        )
                        if not v.admitted:
                            assert v.free_slots == 0


def test_adaptive_slot_cap_a_full_cap_is_a_refusal_not_a_silent_zero() -> None:
    v = judge(cores=32, total=512.0, avail=500.0, load=0.0, running=500)
    assert not v.admitted
    assert any("slot cap" in r for r in v.refusals)


def test_error_overlay_missing_a_limit_refuses_naming_the_field(
    host_capacity_overlay: Path,
) -> None:
    text = host_capacity_overlay.read_text(encoding="utf-8")
    host_capacity_overlay.write_text(
        "\n".join(
            line
            for line in text.splitlines()
            if not line.startswith("max_load_per_core")
        ),
        encoding="utf-8",
    )
    with pytest.raises(NodeOverlayError) as err:
        load_host_capacity_limits()
    assert "max_load_per_core" in str(err.value)


def test_error_overlay_absent_refuses_naming_how_to_supply_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS")
    with pytest.raises(NodeOverlayError) as err:
        load_host_capacity_limits()
    assert "host_capacity" in str(err.value)


def test_error_a_slot_load_under_the_admission_bar_is_refused_when_the_overlay_loads() -> (
    None
):
    with pytest.raises(
        ValueError, match="slot_load_per_core is under max_load_per_core"
    ):
        ModelHostCapacityLimits.model_validate(
            {
                **limits().model_dump(),
                "slot_load_per_core": limits().max_load_per_core / 2,
            }
        )


def test_error_a_ceiling_under_the_floor_is_refused_when_the_overlay_loads() -> None:
    with pytest.raises(ValueError, match="slot_ceiling is under slot_floor"):
        ModelHostCapacityLimits.model_validate(
            {
                **limits().model_dump(),
                "hosts": {"h1": {"slot_floor": 3, "slot_ceiling": 2}},
            }
        )
