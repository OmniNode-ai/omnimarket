# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pacing tests on a fake clock; nothing really sleeps (OMN-20087)."""

from __future__ import annotations

import asyncio

import pytest

from omnimarket.nodes.node_trajectory_evaluation_effect.handlers.pacer import (
    Pacer,
    contract_pacing,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.models.model_trajectory_evaluation import (
    ModelTrajectoryEvaluationAccepted,
    ModelTrajectoryEvaluationRefused,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.protocols import (
    EvaluatorRejectedError,
)
from tests.unit.nodes.node_trajectory_evaluation_effect.fakes import (
    FakeClock,
    full_cmd,
    make_handler,
    submit_cmd,
)

pytestmark = pytest.mark.unit


def _max_in_any_minute(times: list[float]) -> int:
    ts = sorted(times)
    return max(
        (sum(1 for u in ts if t <= u < t + 60) for t in ts),
        default=0,
    )


def test_contract_pacing_starting_values() -> None:
    p = contract_pacing()
    assert (p.submits_per_minute, p.status_reads_per_minute, p.max_in_flight) == (
        30,
        20,
        4,
    )
    assert (p.daily_reserved_credits, p.credits_per_full_text_command) == (5000, 250)
    assert p.credits_per_metadata_command == 0


async def test_sixty_submits_are_paced_and_none_dropped() -> None:
    handler, _, ev, clock = make_handler("in_memory")
    outcomes = await asyncio.gather(*(handler.handle(submit_cmd()) for _ in range(60)))
    assert len(outcomes) == 60
    assert all(isinstance(o, ModelTrajectoryEvaluationAccepted) for o in outcomes)
    assert len(ev.submit_times) == 60
    assert sum(1 for t in ev.submit_times if t < 60) <= 30
    assert _max_in_any_minute(ev.submit_times) <= 30
    assert ev.max_in_flight <= 4
    assert _max_in_any_minute(ev.read_times) <= 20
    assert clock.sleeps  # the pacer waited rather than dropped


async def test_status_reads_never_exceed_twenty_a_minute() -> None:
    clock = FakeClock()
    pacer = Pacer(contract_pacing(), clock)
    times = []
    for _ in range(45):
        await pacer.acquire_status_read()
        times.append(clock.t)
    assert _max_in_any_minute(times) <= 20


async def test_in_flight_is_capped_at_four() -> None:
    clock = FakeClock()
    pacer = Pacer(contract_pacing(), clock)
    live = peak = 0

    async def one() -> None:
        nonlocal live, peak
        async with pacer.submit_slot():
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            live -= 1

    await asyncio.gather(*(one() for _ in range(12)))
    assert peak <= 4


async def test_reservation_refuses_twenty_first_full_text_command() -> None:
    handler, _, ev, _ = make_handler("in_memory")
    for _ in range(20):
        assert isinstance(
            await handler.handle(full_cmd()), ModelTrajectoryEvaluationAccepted
        )
    before = len(ev.submissions)
    out = await handler.handle(full_cmd())
    assert isinstance(out, ModelTrajectoryEvaluationRefused)
    assert out.reason == "credit_reservation_exhausted"
    assert len(ev.submissions) == before
    assert isinstance(
        await handler.handle(submit_cmd()), ModelTrajectoryEvaluationAccepted
    )
    assert len(ev.submissions) == before + 1


async def test_reservation_resets_on_the_next_utc_day() -> None:
    handler, _, _, clock = make_handler("in_memory")
    for _ in range(20):
        await handler.handle(full_cmd())
    assert isinstance(
        await handler.handle(full_cmd()), ModelTrajectoryEvaluationRefused
    )
    clock.t += 12 * 3600  # 2026-09-30T12:00Z -> 2026-10-01T00:00Z
    assert isinstance(
        await handler.handle(full_cmd()), ModelTrajectoryEvaluationAccepted
    )


async def test_reservation_released_when_submit_fails_retryably() -> None:
    from omnimarket.nodes.node_trajectory_evaluation_effect.protocols import (
        EvaluatorTransportError,
    )

    handler, _, ev, _ = make_handler("in_memory")
    ev.fail_with = EvaluatorTransportError("down")
    for _ in range(25):
        await handler.handle(full_cmd())
    ev.fail_with = None
    for _ in range(20):
        assert isinstance(
            await handler.handle(full_cmd()), ModelTrajectoryEvaluationAccepted
        )


async def test_reservation_credit_stop_after_a_402_keeps_metadata_flowing() -> None:
    handler, _, ev, clock = make_handler("in_memory")
    ev.fail_with = EvaluatorRejectedError(
        "payment required", status_code=402, credit_exhausted=True
    )
    out = await handler.handle(full_cmd())
    assert isinstance(out, ModelTrajectoryEvaluationRefused)
    assert out.reason == "target_rejected"
    ev.fail_with = None
    stopped = await handler.handle(full_cmd())
    assert isinstance(stopped, ModelTrajectoryEvaluationRefused)
    assert stopped.reason == "credit_stop"
    assert isinstance(
        await handler.handle(submit_cmd()), ModelTrajectoryEvaluationAccepted
    )
    clock.t += 12 * 3600
    assert isinstance(
        await handler.handle(full_cmd()), ModelTrajectoryEvaluationAccepted
    )
