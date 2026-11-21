# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The swap bar judges swap-in and swap-out activity, never swap used (OMN-26363).

A lab host read 2026-11-21 holds 60 GiB of 200 GiB swap (pages of idle services, history), moves 3.7 MB/s in and 5.6 MB/s out
over 30 one-second samples and has about 60 GiB of memory available. The orchestrator kept lanes off it on the word "swapping".
Every path that admits a host to lanes reads the paging rates (``vmstat`` si and so) and the memory available; the swap held is
never a refusal. Each test names its path: the memory admission node, the capacity node and the placement's host-swapping
refusal. ``test_control_*`` shows each test can fail: a judge that reads swap used refuses the same host.
"""

from __future__ import annotations

import pytest

from omnimarket.handlers.host_capacity_overlay import load_host_capacity_limits
from omnimarket.models.model_host_capacity import (
    ModelHostCapacityLimits,
    ModelHostCapacityReading,
    ModelHostCapacityRequest,
)
from omnimarket.models.model_host_pool_reading import ModelHostPoolReading
from omnimarket.models.remote_lane_placement import ModelRemoteLaneHostReading
from omnimarket.nodes.node_host_capacity_compute import HandlerHostCapacity
from omnimarket.nodes.node_host_memory_admission_compute.handlers.handler_host_memory_admission import (
    HandlerHostMemoryAdmission,
)
from omnimarket.nodes.node_host_memory_admission_compute.models.model_host_memory_admission import (
    ModelHostMemoryAdmissionRequest,
)
from omnimarket.nodes.node_host_pool_alarm_effect.handler import swap_reasons
from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    swap_refusal,
)

pytestmark = pytest.mark.unit

GIB = 1024 * 1024  # KiB in a GiB

#: The host as read 2026-11-21: 59 GiB of 199 GiB swap held, 58 GiB of 92 GiB memory available, paging idle.
SWAP_USED_GIB = 59.0
SWAP_TOTAL_GIB = 199.0
MEM_AVAIL_GIB = 58.0
MEM_TOTAL_GIB = 92.0
SWAP_USED_FRAC = SWAP_USED_GIB / SWAP_TOTAL_GIB
#: A bar a host that pages over the lab's swap-in bound is refused at.
SWAP_IN_BAR_KB_S = 20000.0


def admission(
    *, swap_in: float | None, swap_out: float | None, avail: float = MEM_AVAIL_GIB
) -> tuple[str, ...]:
    return (
        HandlerHostMemoryAdmission()
        .handle(
            ModelHostMemoryAdmissionRequest(
                mem_avail_gb=avail,
                mem_psi_avg60=1.3,
                swap_in_kb_s=swap_in,
                swap_out_kb_s=swap_out,
                min_free_gb=10.0,
                max_mem_psi=5.0,
                max_swap_kb_s=100000.0,
                max_swap_in_kb_s=SWAP_IN_BAR_KB_S,
            )
        )
        .refusals
    )


def test_swap_activity_not_swap_used_memory_admission_admits_historic_swap_at_zero_swap_in() -> (
    None
):
    # 59 GiB of swap held and none moving: the request has no swap-used field to judge, only the rates and the memory available.
    assert admission(swap_in=0.0, swap_out=0.0) == ()


def test_swap_activity_not_swap_used_memory_admission_refuses_swap_in_over_the_bar() -> (
    None
):
    refusals = admission(swap_in=SWAP_IN_BAR_KB_S + 1000.0, swap_out=0.0)
    assert refusals
    assert "swap-in" in refusals[0]


def test_swap_activity_not_swap_used_memory_admission_refuses_swap_traffic_over_the_combined_bar() -> (
    None
):
    refusals = admission(swap_in=1000.0, swap_out=100000.0)
    assert any("swap traffic" in r for r in refusals)


def test_swap_activity_not_swap_used_memory_admission_judges_available_memory_beside_the_rates() -> (
    None
):
    refusals = admission(swap_in=0.0, swap_out=0.0, avail=4.0)
    assert refusals
    assert "mem_avail" in refusals[0]


def capacity(
    *,
    swap_in: float | None,
    swap_out: float | None,
    limits: ModelHostCapacityLimits | None = None,
):
    return HandlerHostCapacity().handle(
        ModelHostCapacityRequest(
            reading=ModelHostCapacityReading(
                host="h201",
                logical_cores=32,
                mem_total_gb=MEM_TOTAL_GIB,
                mem_avail_gb=MEM_AVAIL_GIB,
                load_one_minute=5.7,
                running_lanes=1,
                swap_in_kb_s=swap_in,
                swap_out_kb_s=swap_out,
            ),
            limits=limits or load_host_capacity_limits(),
        )
    )


@pytest.mark.usefixtures("host_capacity_overlay")
def test_swap_activity_not_swap_used_capacity_admits_historic_swap_at_zero_swap_in() -> (
    None
):
    verdict = capacity(swap_in=0.0, swap_out=0.0)
    assert verdict.admitted, verdict.refusals
    assert verdict.free_slots >= 1
    assert verdict.binding != "swap"


@pytest.mark.usefixtures("host_capacity_overlay")
def test_swap_activity_not_swap_used_capacity_refuses_swap_in_over_the_bar() -> None:
    limits = load_host_capacity_limits()
    verdict = capacity(swap_in=limits.max_swap_in_kb_s + 1000.0, swap_out=0.0)
    assert not verdict.admitted
    assert verdict.binding == "swap"
    assert "paging" in verdict.refusals[0]
    assert verdict.free_slots == 0


def pool_reading(swap_in: float | None) -> ModelHostPoolReading:
    return ModelHostPoolReading(
        host="h201",
        lane_cap=7,
        error=None,
        swap_used_frac=SWAP_USED_FRAC,
        swap_in_kb_s=swap_in,
        swap_out_kb_s=0.0,
        mem_psi_avg60=1.3,
    )


def test_swap_activity_not_swap_used_host_pool_alarm_ignores_swap_held() -> None:
    # The reading carries the 30 percent swap held and the alarm still names no reason at zero swap-in.
    assert pool_reading(0.0).swap_used_frac == pytest.approx(SWAP_USED_FRAC)
    assert swap_reasons(pool_reading(0.0)) == []
    over = swap_reasons(pool_reading(SWAP_IN_BAR_KB_S + 1000.0))
    assert over
    assert "swap-in" in over[0]


def test_swap_activity_not_swap_used_placement_admits_a_host_with_historic_swap() -> (
    None
):
    # The placement's reading holds the rates and PSI only: there is no swap-used field for the refusal to read.
    assert "swap_used" not in ModelRemoteLaneHostReading.model_fields
    clean = ModelRemoteLaneHostReading(
        name="h201", lane_cap=7, swap_in_kb_s=0.0, mem_psi_avg60=1.3
    )
    assert swap_refusal(clean) is None


def test_swap_activity_not_swap_used_placement_refuses_swap_in_over_the_bar() -> None:
    reading = ModelRemoteLaneHostReading(
        name="h201",
        lane_cap=7,
        swap_in_kb_s=SWAP_IN_BAR_KB_S + 1000.0,
        mem_psi_avg60=1.3,
    )
    refusal = swap_refusal(reading)
    assert refusal is not None
    assert refusal.startswith("host-swapping: swap-in")


def test_swap_activity_not_swap_used_placement_refusal_is_the_open_alarm_not_swap_held() -> (
    None
):
    # The one other clause that keeps a host off at zero paging: the pool alarm still open until two clean reads. It is named.
    reading = ModelRemoteLaneHostReading(
        name="h201",
        lane_cap=7,
        swap_in_kb_s=0.0,
        mem_psi_avg60=1.3,
        swap_alarm_open=True,
    )
    assert swap_refusal(reading) == "host-swapping: alarm open until two clean reads"


def judge_by_swap_used(swap_used_gib: float, swap_total_gib: float) -> bool:
    """The defect the bar must not have: a host admitted only while under a share of its swap held."""
    return swap_used_gib / swap_total_gib <= 0.25


def test_control_a_judge_that_reads_swap_used_refuses_the_host_these_tests_admit() -> (
    None
):
    assert not judge_by_swap_used(SWAP_USED_GIB, SWAP_TOTAL_GIB)
    assert admission(swap_in=0.0, swap_out=0.0) == ()
    # and the same host at an unread rate is never judged on paging (None is never refused), so a probe gap is not a swap-used read
    assert admission(swap_in=None, swap_out=None) == ()
