# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PR watch polls at most once per interval and refuses a poll inside it (OMN-21033).

GitHub quota is the scarce resource a watch spends, so the interval is a hard
floor on the spacing of two reads of the same PR, not a hint: a sleep that wakes
early, a deadline that lands inside the interval, or a caller that asks for a
second read too soon must not produce one.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from omnimarket.nodes.node_pr_watch_orchestrator.handlers.handler_pr_watch_orchestrator import (
    HandlerPrWatchOrchestrator,
    PrWatchPollRefusedError,
    PrWatchPollSchedule,
)
from omnimarket.nodes.node_pr_watch_orchestrator.models import (
    EnumPrCheckBucket,
    EnumPrWatchConclusion,
    ModelPrCheckStatus,
    ModelPrWatchOrchestratorRequest,
)

pytestmark = pytest.mark.unit

REPO = "OmniNode-ai/omnimarket"
LONGEST_INTERVAL_SECONDS = 600.0


class _Clock:
    """A monotonic clock the fake sleep advances, optionally waking early."""

    def __init__(self, *, max_step: float = float("inf")) -> None:
        self.now = 1000.0
        self.max_step = max_step
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += min(seconds, self.max_step)


class _PendingThenGreen:
    def __init__(self, clock: _Clock, pending_polls: int) -> None:
        self._clock = clock
        self._pending_polls = pending_polls
        self.poll_times: list[float] = []

    def fetch_checks(
        self, request: ModelPrWatchOrchestratorRequest
    ) -> tuple[ModelPrCheckStatus, ...]:
        self.poll_times.append(self._clock.now)
        bucket = (
            EnumPrCheckBucket.PENDING
            if len(self.poll_times) <= self._pending_polls
            else EnumPrCheckBucket.PASS
        )
        return (ModelPrCheckStatus(name="unit", bucket=bucket, state=bucket.value),)


def _gaps(times: list[float]) -> list[float]:
    return [later - earlier for earlier, later in pairwise(times)]


async def test_pr_watch_interval_wakes_early_but_never_polls_inside_the_interval() -> (
    None
):
    clock = _Clock(max_step=3.0)
    client = _PendingThenGreen(clock, pending_polls=2)
    handler = HandlerPrWatchOrchestrator(
        checks_client=client, monotonic=clock.monotonic, sleep=clock.sleep
    )

    result = await handler.handle(
        ModelPrWatchOrchestratorRequest(
            repo=REPO, pr_number=1, poll_interval_seconds=10, timeout_seconds=500
        )
    )

    assert result.conclusion is EnumPrWatchConclusion.GREEN
    assert len(client.poll_times) == 3
    assert all(gap >= 10 for gap in _gaps(client.poll_times)), client.poll_times


async def test_pr_watch_interval_longest_contract_interval_is_one_read_per_interval() -> (
    None
):
    clock = _Clock()
    client = _PendingThenGreen(clock, pending_polls=99)
    handler = HandlerPrWatchOrchestrator(
        checks_client=client, monotonic=clock.monotonic, sleep=clock.sleep
    )

    result = await handler.handle(
        ModelPrWatchOrchestratorRequest(
            repo=REPO,
            pr_number=1,
            poll_interval_seconds=LONGEST_INTERVAL_SECONDS,
            timeout_seconds=1300,
        )
    )

    assert result.conclusion is EnumPrWatchConclusion.TIMEOUT
    assert result.attempts == 3, "1300 s at one read per 600 s is reads at 0, 600, 1200"
    assert _gaps(client.poll_times) == [LONGEST_INTERVAL_SECONDS] * 2
    assert result.elapsed_seconds == pytest.approx(1300)


async def test_pr_watch_interval_deadline_inside_the_interval_ends_without_another_read() -> (
    None
):
    clock = _Clock()
    client = _PendingThenGreen(clock, pending_polls=99)
    handler = HandlerPrWatchOrchestrator(
        checks_client=client, monotonic=clock.monotonic, sleep=clock.sleep
    )

    result = await handler.handle(
        ModelPrWatchOrchestratorRequest(
            repo=REPO,
            pr_number=1,
            poll_interval_seconds=LONGEST_INTERVAL_SECONDS,
            timeout_seconds=100,
        )
    )

    assert result.conclusion is EnumPrWatchConclusion.TIMEOUT
    assert len(client.poll_times) == 1, "a read at the deadline would be inside 600 s"
    assert result.attempts == 1
    assert result.pending_checks == ("unit",)


def test_pr_watch_interval_schedule_refuses_a_poll_inside_the_interval() -> None:
    schedule = PrWatchPollSchedule(interval_seconds=10.0)

    schedule.claim(now=100.0)
    with pytest.raises(PrWatchPollRefusedError, match="7"):
        schedule.claim(now=103.0)
    with pytest.raises(PrWatchPollRefusedError):
        schedule.claim(now=109.999)
    schedule.claim(now=110.0)

    assert schedule.wait_needed(now=111.0) == pytest.approx(9.0)


def test_pr_watch_interval_zero_allows_back_to_back_polls() -> None:
    schedule = PrWatchPollSchedule(interval_seconds=0.0)

    schedule.claim(now=5.0)
    schedule.claim(now=5.0)
