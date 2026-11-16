# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A filler pass ends inside its wall-clock budget and resumes where it stopped (OMN-22518).

Measured 2026-11-09 on h051 against the live PR watcher state: 886 merge-relevant runs, the deployed
overlay (concurrency 6, 30 reads). The gateway answers cache probes one at a time, so a probe took a median
2.4 s under load and 227 probes took 142 s; probing every run before the first fill meant the pass never
reached a fill and the unit's 360 s limit ended it (exit 124) before the held-memo was written, so the next
pass started from nothing. These tests pin the repair with a fake clock and a reader that spends time:

* the pass stops starting reads once its budget is spent, and says how many runs it left (the failing
  control: with no budget the pass spends every probe and ends far past it);
* newest failures are served first, so a fresh failure is filled inside the budget however large the backlog;
* the next pass resumes: logs the last pass confirmed are not probed again, so a backlog drains.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from omnimarket.models.model_github_gateway_io import ModelJobLogTailResult
from omnimarket.nodes.node_job_log_filler_effect.handlers.handler_job_log_filler import (
    HandlerJobLogFiller,
)
from omnimarket.nodes.node_job_log_filler_effect.models.model_job_log_filler import (
    EnumJobLogFillOutcome,
    ModelJobLogFillRequest,
)
from omnimarket.nodes.node_job_log_filler_effect.protocols.protocol_job_log_reader import (
    JobLogCacheMissError,
)
from tests.nodes.node_job_log_filler_effect.planted import _record, overlay

pytestmark = pytest.mark.unit

PROBE_S = 3.0
FILL_S = 10.0
BASE_JOB = 128000001000


class SlowClock:
    """Time that only a reader's work moves."""

    def __init__(self) -> None:
        self.now = 1_000_000.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        return self.now

    def spend(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


class SlowReader:
    """Every log is missing until filled; a probe costs PROBE_S and a fill FILL_S of the clock."""

    cache_path: str | None = None

    def __init__(self, clock: SlowClock) -> None:
        self._clock = clock
        self.cached: set[int] = set()
        self.probes: list[int] = []
        self.fills: list[int] = []

    def read(
        self,
        repo: str,
        job_id: int,
        *,
        cache_only: bool,
        tail_bytes: int | None = None,
    ) -> ModelJobLogTailResult:
        if cache_only:
            self._clock.spend(PROBE_S)
            self.probes.append(job_id)
            if job_id not in self.cached:
                raise JobLogCacheMissError(f"JOB-LOG-CACHE-MISS {job_id}")
        else:
            self._clock.spend(FILL_S)
            self.fills.append(job_id)
            self.cached.add(job_id)
        return ModelJobLogTailResult(
            repo=repo, job_id=job_id, total_bytes=10, truncated=False, text="x"
        )


def backlog(count: int) -> dict[str, Any]:
    """``count`` red pull requests, pull request n completed n minutes after the first: the last is the newest."""
    prs: dict[str, Any] = {}
    for n in range(1, count + 1):
        prs[f"omnimarket#{n}"] = _record(
            n,
            "red",
            red=["Tests Gate"],
            detail=[
                [
                    "Tests Gate",
                    str(BASE_JOB + n),
                    f"2026-11-09T08:{n:02d}:00Z",
                ]
            ],
        )
    return {"last_tick": "2026-11-09T09:00:00Z", "prs": prs}


def handler(
    count: int, *, budget: float, reads: int = 100
) -> tuple[HandlerJobLogFiller, SlowReader, SlowClock]:
    clock = SlowClock()
    reader = SlowReader(clock)
    return (
        HandlerJobLogFiller(
            overlay=overlay(pass_budget_seconds=budget, max_reads_per_pass=reads),
            reader_factory=lambda _profile: reader,
            state_reader=lambda: backlog(count),
            clock=clock,
        ),
        reader,
        clock,
    )


def test_a_pass_ends_inside_its_budget_and_counts_what_it_left() -> None:
    h, _, clock = handler(40, budget=60.0)
    start = clock()
    result = h.fill(ModelJobLogFillRequest())
    # 60 s buys 4 runs (probe 3 + fill 10 = 13 s each); the run in flight when the budget ends is finished.
    assert clock() - start <= 60.0 + PROBE_S + FILL_S
    assert result.budget_exhausted is True
    assert result.filled + result.already_cached + result.failed < result.considered
    assert result.deferred == result.considered - result.filled - result.already_cached
    assert result.considered == 40


def test_the_newest_failures_are_filled_first() -> None:
    h, reader, _ = handler(40, budget=60.0)
    result = h.fill(ModelJobLogFillRequest())
    newest = [BASE_JOB + n for n in range(40, 40 - result.filled, -1)]
    assert result.filled >= 4
    assert reader.fills == newest  # newest completion first, nothing older before them


def test_the_next_pass_resumes_after_the_runs_the_last_pass_confirmed() -> None:
    h, reader, _ = handler(40, budget=60.0)
    first = h.fill(ModelJobLogFillRequest())
    reader.probes.clear()
    second = h.fill(ModelJobLogFillRequest())
    first_done = {i.run.job_id for i in first.items}
    # Whatever the first pass confirmed is not probed or read again; the second pass goes on to older runs.
    assert first_done.isdisjoint(set(reader.probes))
    assert second.already_cached >= first.filled + first.already_cached
    assert second.filled >= 1


def test_a_backlog_drains_in_bounded_passes() -> None:
    h, reader, clock = handler(40, budget=60.0)
    passes = 0
    while True:
        start = clock()
        result = h.fill(ModelJobLogFillRequest())
        assert clock() - start <= 60.0 + PROBE_S + FILL_S
        passes += 1
        if not result.deferred:
            break
        assert passes < 20
    assert len(reader.cached) == 40
    assert passes > 1


def test_a_pass_with_no_budget_pressure_is_unchanged() -> None:
    h, _, _ = handler(3, budget=3600.0)
    result = h.fill(ModelJobLogFillRequest())
    assert (result.filled, result.deferred, result.budget_exhausted) == (3, 0, False)
    assert {i.outcome for i in result.items} == {EnumJobLogFillOutcome.FILLED}
