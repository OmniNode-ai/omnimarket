# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host the throughput tick's host-swapping check flags has zero free lane slots (OMN-17232).

At 2026-10-30T01:25Z a lab host paged at load 30 to 58 with swap-in up to 167 MB/s while the pool counted
it 6 to 9 free Claude slots, so the overflow order (lab hosts first, then a cloud session) never reached
cloud. The count now follows the tick's own check, the host pool alarm's ``swap_reasons``, and the
alarm's open state between bursts.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_host_pool_alarm_effect.models import (
    MEM_PRESSURE_CRITICAL,
    MEM_PSI_BOUND,
    SWAP_IN_BOUND_KB_S,
)
from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
    effective_lane_slots,
    pool_lane_slots,
    swap_refusal,
)
from omnimarket.nodes.node_remote_lane_compute.models import (
    EnumRemoteLaneOutcome,
    ModelRemoteLaneHostReading,
    ModelRemoteLanePlacementRequest,
)

PAGING_SWAP_IN_KB_S = 167_000.0  # the measured burst
BELOW_BOUND_KB_S = SWAP_IN_BOUND_KB_S - 1.0


def _reading(name: str = "h201", **kwargs: object) -> ModelRemoteLaneHostReading:
    base: dict[str, object] = {
        "name": name,
        "engines": ("claude_sonnet", "codex"),
        "lane_slots": 9,
        "lane_cap": 10,
        "rank_free": 5.0,
    }
    base.update(kwargs)
    return ModelRemoteLaneHostReading.model_validate(base)


@pytest.mark.unit
@pytest.mark.parametrize(
    "paging",
    [
        {"swap_in_kb_s": PAGING_SWAP_IN_KB_S},
        {"swap_in_kb_s": SWAP_IN_BOUND_KB_S + 1.0},
        {"mem_psi_avg60": MEM_PSI_BOUND + 0.5},
        {"mem_pressure": MEM_PRESSURE_CRITICAL},
        {"swap_alarm_open": True},
    ],
)
def test_a_paging_host_gets_zero_slots(paging: dict[str, object]) -> None:
    reading = _reading(**paging)
    assert swap_refusal(reading) is not None
    assert effective_lane_slots(reading) == 0
    assert pool_lane_slots((reading,)) == {"h201": 0}


@pytest.mark.unit
@pytest.mark.parametrize(
    "recovered",
    [
        {},
        {"swap_in_kb_s": 0.0},
        {"swap_in_kb_s": BELOW_BOUND_KB_S},
        {"swap_in_kb_s": 1920.0, "mem_psi_avg60": 0.02, "mem_pressure": 1},
        {"swap_alarm_open": False},
    ],
)
def test_a_recovered_host_gets_its_slots_back(recovered: dict[str, object]) -> None:
    reading = _reading(**recovered)
    assert swap_refusal(reading) is None
    assert effective_lane_slots(reading) == 9


@pytest.mark.unit
def test_the_bound_is_the_alarms_not_a_copy() -> None:
    assert swap_refusal(_reading(swap_in_kb_s=SWAP_IN_BOUND_KB_S)) is None
    assert swap_refusal(_reading(swap_in_kb_s=SWAP_IN_BOUND_KB_S + 0.1)) is not None


@pytest.mark.unit
def test_pool_count_zeroes_only_the_paging_host() -> None:
    readings = (
        _reading("h201", swap_in_kb_s=PAGING_SWAP_IN_KB_S, lane_slots=9),
        _reading("h200", lane_slots=3),
        _reading("h051", lane_slots=8, swap_alarm_open=True),
    )
    assert pool_lane_slots(readings) == {"h201": 0, "h200": 3, "h051": 0}


@pytest.mark.unit
@pytest.mark.parametrize("engine", ["claude_sonnet", "codex"])
def test_a_paging_host_takes_no_lane_and_says_why(engine: str) -> None:
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(
            engine=engine,
            readings=(_reading(swap_in_kb_s=PAGING_SWAP_IN_KB_S),),
        )
    )
    assert result.host is None
    assert result.outcome == EnumRemoteLaneOutcome.NO_HOST
    (verdict,) = result.verdicts
    assert verdict.host == "h201"
    assert verdict.reason.startswith("host-swapping: swap-in 167 MB/s over 20 MB/s")


@pytest.mark.unit
def test_a_quieter_host_takes_the_lane_while_the_other_pages() -> None:
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(
            engine="claude_sonnet",
            readings=(
                _reading("h201", swap_in_kb_s=PAGING_SWAP_IN_KB_S, lane_slots=9),
                _reading("h200", lane_slots=1, rank_free=1.0),
            ),
        )
    )
    assert result.host == "h200"
    reasons = {v.host: v.reason for v in result.verdicts}
    assert reasons["h201"].startswith("host-swapping:")


@pytest.mark.unit
def test_the_host_places_again_once_the_alarm_resolves() -> None:
    handler = HandlerRemoteLanePlacement()

    def place(**state: object) -> str | None:
        return handler.handle(
            ModelRemoteLanePlacementRequest(
                engine="claude_sonnet", readings=(_reading(**state),)
            )
        ).host

    assert place(swap_in_kb_s=PAGING_SWAP_IN_KB_S, swap_alarm_open=True) is None
    # a clean read while the alarm is still open (it resolves after two) stays off
    assert place(swap_in_kb_s=0.0, swap_alarm_open=True) is None
    assert place(swap_in_kb_s=0.0, swap_alarm_open=False) == "h201"


@pytest.mark.unit
def test_an_old_producer_without_swap_readings_places_as_before() -> None:
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(
            engine="claude_sonnet",
            readings=(
                ModelRemoteLaneHostReading(
                    name="h201",
                    engines=("claude_sonnet",),
                    lane_slots=2,
                    lane_cap=4,
                ),
            ),
        )
    )
    assert result.host == "h201"
