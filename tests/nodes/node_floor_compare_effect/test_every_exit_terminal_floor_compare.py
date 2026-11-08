# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every way out of the comparison cycle ends its start in a success or a typed failure (OMN-22864).

Measured on the served floor of 2026-11-08 08:09Z: the chains row names
``piece 17 node_floor_compare_effect:run_floor_compare_cycle: start_without_terminal`` 900 s after a start, while the same
publish tick had written a current comparison. The publish node runs the cycle as a step with a 15 s limit and ends a
step past it with a kill, which no process can answer; a cycle that took a termination signal had no handler either.

Golden chain: a cycle that finishes leaves no timer and no handler behind. Error chain (the failing controls): a
termination signal and the cycle's own deadline each end the cycle in a typed failure, and the real receipt reader then
finds no open chain; with the guard dropped (the named injected break) it still reads the open start.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers import cycle_interrupt
from omnimarket.handlers.cycle_interrupt import CycleInterrupted
from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.nodes.node_floor_compare_effect.handlers import (
    handler_floor_compare as compare,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelFloorCompareRequest,
)
from tests.nodes.node_floor_compare_effect.support import NOW, RT, build_home
from tests.nodes.node_floor_simple_effect.support import NOW as SIMPLE_NOW
from tests.nodes.node_floor_simple_effect.support import build_home as simple_home
from tests.nodes.node_floor_simple_effect.test_cycle_exits_floor_simple import (
    open_chains,
)

pytestmark = pytest.mark.unit

SUBJECT = f"piece {RT.emission_piece} {RT.emission_node}"


def cycle(home: Path, sink: InMemoryEmissionSink) -> object:
    return compare.HandlerFloorCompare(sink=sink, clock=lambda: NOW).run(
        ModelFloorCompareRequest(home=str(home), now=NOW)
    )


def phases(sink: InMemoryEmissionSink) -> list[tuple[str, str | None]]:
    return [(e.phase, e.failure_code) for e in sink.events]


def open_starts(sink: InMemoryEmissionSink, tmp_path: Path) -> list[str]:
    """The compare chains the real receipt reader reads as a start with no terminal, a long time after the cycle."""
    return [s for s in open_chains(sink, simple_home(tmp_path)) if s == SUBJECT]


def hang_in_the_cycle(monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    def hung(*_args: object, **_kwargs: object) -> object:
        if how == "signal":
            os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(30)
        raise AssertionError("the guard did not interrupt the cycle")

    monkeypatch.setattr(
        compare.HandlerFloorCompare, "_with_sources", staticmethod(hung)
    )


def test_golden_a_cycle_that_finishes_leaves_no_timer_and_no_handler_behind(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    before = (signal.getsignal(signal.SIGALRM), signal.getsignal(signal.SIGTERM))
    assert cycle(build_home(tmp_path), sink).ok
    assert phases(sink) == [("start", None), ("success", None)]
    assert (
        signal.getsignal(signal.SIGALRM),
        signal.getsignal(signal.SIGTERM),
    ) == before
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert open_starts(sink, tmp_path) == []


def test_error_a_termination_signal_ends_the_cycle_in_a_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = InMemoryEmissionSink()
    hang_in_the_cycle(monkeypatch, "signal")
    with pytest.raises(CycleInterrupted) as stopped:
        cycle(build_home(tmp_path), sink)
    assert stopped.value.code == "terminated_by_signal"
    assert phases(sink) == [("start", None), ("failure", "terminated_by_signal")]
    assert open_starts(sink, tmp_path) == []


def test_error_a_cycle_past_its_deadline_ends_itself_in_a_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = InMemoryEmissionSink()
    runtime = RT.model_copy(update={"cycle_deadline_seconds": 1})
    monkeypatch.setattr(compare, "load_runtime", lambda *_a, **_k: runtime)
    hang_in_the_cycle(monkeypatch, "deadline")
    began = time.monotonic()
    with pytest.raises(CycleInterrupted) as overdue:
        cycle(build_home(tmp_path), sink)
    assert time.monotonic() - began < 10
    assert overdue.value.code == "cycle_deadline_exceeded"
    assert phases(sink) == [("start", None), ("failure", "cycle_deadline_exceeded")]
    assert open_starts(sink, tmp_path) == []
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_error_an_unexpected_error_ends_the_cycle_in_a_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = InMemoryEmissionSink()

    def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("the history is unreadable")

    monkeypatch.setattr(compare, "read_history", broken)
    with pytest.raises(RuntimeError):
        cycle(build_home(tmp_path), sink)
    assert phases(sink) == [("start", None), ("failure", "cycle_interrupted")]
    assert open_starts(sink, tmp_path) == []


def test_error_the_start_stays_open_when_the_guard_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The injected break: no signal handler and no deadline; a termination ends the process with the start open.

    The signal is modelled as the process vanishing (a kill no handler answers), which the real receipt reader then
    reads as the finding the board shows.
    """

    @contextlib.contextmanager
    def unguarded(_deadline: int) -> Iterator[None]:
        yield

    monkeypatch.setattr(compare, "interruptible", unguarded)

    class Killed(BaseException):
        pass

    def vanish(*_args: object, **_kwargs: object) -> object:
        raise Killed

    monkeypatch.setattr(
        compare.HandlerFloorCompare, "_with_sources", staticmethod(vanish)
    )
    sink = InMemoryEmissionSink()
    started = SIMPLE_NOW - timedelta(minutes=30)
    with pytest.raises(Killed):
        compare.HandlerFloorCompare(sink=_NoCloseSink(sink), clock=lambda: started).run(
            ModelFloorCompareRequest(home=str(build_home(tmp_path)), now=started)
        )
    assert phases(sink) == [("start", None)]
    assert open_starts(sink, tmp_path) == [SUBJECT]


class _NoCloseSink:
    """A sink that loses every failure and success: the process was gone before it could write one."""

    def __init__(self, inner: InMemoryEmissionSink) -> None:
        self._inner = inner

    def emit(self, event: ModelNodeEmission) -> None:
        if event.phase == "start":
            self._inner.emit(event)

    def read(self, since: datetime) -> tuple[ModelNodeEmission, ...]:
        return self._inner.read(since)


def test_the_cycle_deadline_is_below_the_publish_step_limit() -> None:
    """The deadline, not the publish node's kill, ends a hung cycle; no bound of the publish node is changed."""
    publish = yaml.safe_load(
        (
            Path(compare.__file__).resolve().parents[2]
            / "node_factory_floor_publish_effect"
            / "contract.yaml"
        ).read_text(encoding="utf-8")
    )
    steps = {
        s["name"]: s["timeout_seconds"]
        for key in ("steps", "pre_publish_steps", "after_publish_steps")
        for s in publish["runtime"].get(key, ())
    }
    assert 0 < RT.cycle_deadline_seconds < steps["floor_compare"]
    assert steps["floor_compare"] == 15


def test_the_guard_is_the_shared_one() -> None:
    assert compare.interruptible is cycle_interrupt.interruptible
