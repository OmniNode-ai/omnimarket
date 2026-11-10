# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every way out of a 17a cycle that has started ends in a success or a typed failure (OMN-22605).

Measured on 2026-11-06 at 11:09:30Z: the floor unit ran new overlay data (the dev deploy agent source) on code that did
not know it. The cycle emitted its start, the lab overlay then failed validation outside any step, the process refused
with exit 2 and the journal held a start with no terminal. The chain check read ``start_without_terminal`` on piece 17
for 900 s. Each test below injects one way out (a refused overlay, an error between steps, a termination signal, the
cycle's own deadline) and reads the journal through the real receipt reader: no chain of the cycle may stay open.
"""

from __future__ import annotations

import os
import shutil
import signal
import time
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.cycle_interrupt import CycleInterrupted
from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.floor_17a import (
    ModelFloorSimpleOverlay,
    ModelFloorSimpleTickRequest,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_lab_state import (
    HandlerLabState,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    EMIT_CYCLE,
    HandlerFloorSimpleTick,
    load_runtime,
)
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    OVERLAYS,
    build_home,
    environ,
)
from tests.nodes.node_floor_simple_effect.test_golden_chain_floor_simple import (
    ticking,
)

pytestmark = pytest.mark.unit


def cycle_phases(sink: InMemoryEmissionSink) -> list[tuple[str, str | None]]:
    return [(e.phase, e.failure_code) for e in sink.events if e.node == EMIT_CYCLE]


def open_chains(sink: InMemoryEmissionSink, home: Path) -> list[str]:
    """The nodes that emitted a start and no terminal, read by the real receipt reader a long time after the cycle."""
    env = environ(home)
    floor_ov = load_structured_overlay(
        "node_floor_simple_compute", ModelFloorSimpleOverlay, env, file_env="X"
    )
    late = NOW + timedelta(hours=2)
    receipts = HandlerFloorSimpleTick._receipts(
        sink, floor_ov, late, load_runtime(), None, ()
    )
    return [f.subject for f in receipts.findings if f.code == "start_without_terminal"]


def refused_lab_overlay_env(home: Path, tmp_path: Path) -> dict[str, str]:
    """The lab overlay carries a field the code does not know, as the live overlay did before the code was installed."""
    roots = tmp_path / "overlays"
    shutil.copytree(OVERLAYS, roots)
    lab = roots / "node_floor_simple_effect" / "overlay.yaml"
    lab.write_text(
        lab.read_text(encoding="utf-8") + "\na_setting_the_code_does_not_know: 1\n"
    )
    return {**environ(home), "ONEX_SKILL_OVERLAY_ROOTS": str(roots)}


def handler(sink: InMemoryEmissionSink) -> HandlerFloorSimpleTick:
    return HandlerFloorSimpleTick(
        sink=sink, clock=ticking(), served_alarm_ids=lambda: None
    )


def test_error_a_lab_overlay_the_code_does_not_know_closes_the_started_cycle_with_a_typed_failure(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    sink = InMemoryEmissionSink()
    env = refused_lab_overlay_env(home, tmp_path)
    with pytest.raises(NodeOverlayError, match="a_setting_the_code_does_not_know"):
        handler(sink).run(ModelFloorSimpleTickRequest(home=str(home)), environ=env)
    assert cycle_phases(sink) == [("start", None), ("failure", "overlay_refused")]
    failure = sink.events[-1]
    assert "a_setting_the_code_does_not_know" in failure.failure_detail
    assert open_chains(sink, home) == []


def test_error_an_error_between_steps_closes_the_started_cycle_and_returns_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = build_home(tmp_path)
    sink = InMemoryEmissionSink()

    def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("the history file is a directory")

    monkeypatch.setattr(HandlerFloorSimpleTick, "_history", staticmethod(broken))
    result = handler(sink).run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=environ(home)
    )
    assert not result.ok
    assert result.failure_code == "unexpected_RuntimeError"
    assert cycle_phases(sink)[-1] == ("failure", "unexpected_RuntimeError")
    assert open_chains(sink, home) == []


def test_error_a_termination_signal_closes_the_cycle_and_the_step_it_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = build_home(tmp_path)
    sink = InMemoryEmissionSink()

    def stopped_in_the_step(*_args: object, **_kwargs: object) -> object:
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)
        raise AssertionError("the signal did not interrupt the step")

    monkeypatch.setattr(HandlerLabState, "handle", stopped_in_the_step)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(CycleInterrupted) as stopped:
        handler(sink).run(
            ModelFloorSimpleTickRequest(home=str(home)), environ=environ(home)
        )
    assert stopped.value.code == "terminated_by_signal"
    assert cycle_phases(sink)[-1] == ("failure", "terminated_by_signal")
    step_failure = next(
        e
        for e in sink.events
        if e.node.endswith(":compute_lab_state") and e.phase == "failure"
    )
    assert step_failure.failure_code == "terminated_by_signal"
    assert open_chains(sink, home) == []
    assert signal.getsignal(signal.SIGTERM) is before


def test_error_a_cycle_past_its_deadline_ends_itself_with_a_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = build_home(tmp_path)
    sink = InMemoryEmissionSink()
    runtime = load_runtime().model_copy(update={"cycle_deadline_seconds": 1})
    monkeypatch.setattr(
        "omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick.load_runtime",
        lambda *_a, **_k: runtime,
    )

    def hung(*_args: object, **_kwargs: object) -> object:
        time.sleep(30)
        raise AssertionError("the deadline did not interrupt the step")

    monkeypatch.setattr(HandlerLabState, "handle", hung)
    began = time.monotonic()
    with pytest.raises(CycleInterrupted) as overdue:
        handler(sink).run(
            ModelFloorSimpleTickRequest(home=str(home)), environ=environ(home)
        )
    assert time.monotonic() - began < 10
    assert overdue.value.code == "cycle_deadline_exceeded"
    assert cycle_phases(sink)[-1] == ("failure", "cycle_deadline_exceeded")
    assert open_chains(sink, home) == []


def test_golden_a_cycle_that_finishes_leaves_no_timer_and_no_handler_behind(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    sink = InMemoryEmissionSink()
    before = signal.getsignal(signal.SIGALRM)
    result = handler(sink).run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=environ(home)
    )
    assert result.ok
    assert cycle_phases(sink)[-1] == ("success", None)
    assert signal.getsignal(signal.SIGALRM) is before
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert open_chains(sink, home) == []


def test_error_an_overlay_that_fails_before_the_start_emits_nothing(
    tmp_path: Path,
) -> None:
    """No start, so nothing can be left open: the refusal is the exit status and the stderr line alone."""
    home = build_home(tmp_path)
    sink = InMemoryEmissionSink()
    with pytest.raises(NodeOverlayError):
        HandlerFloorSimpleTick(sink=sink).run(
            ModelFloorSimpleTickRequest(home=str(home)),
            environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "nowhere")},
        )
    assert sink.events == []


def test_golden_the_contract_operation_dispatches_to_a_handle_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto-wiring binds ``run_floor_simple_cycle`` to ``handle``; a handler with only ``run`` raises on every dispatch."""
    home = build_home(tmp_path)
    for name, value in environ(home).items():
        monkeypatch.setenv(name, value)
    sink = InMemoryEmissionSink()
    result = handler(sink).handle(ModelFloorSimpleTickRequest(home=str(home)))
    assert result.ok
    assert cycle_phases(sink)[-1] == ("success", None)
