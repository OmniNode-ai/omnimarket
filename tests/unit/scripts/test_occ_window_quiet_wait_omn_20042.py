# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20042: the receipt runner does not push into a batch window mid-run.

The OCC receipt runner pushes an ``evidence(OMN-16859)`` commit onto the
product PR's companion branch. When that branch is the repository's batch
window (``auto/window-*``) and the window's change-control CI is in flight,
the push cancels that run and restarts it, so a busy window never merges and
holds every member PR behind it. The companion emitter already defers its own
window rebuilds for the same reason; ``scripts/ci/occ_window_quiet_wait.py``
applies the same guard to the runner's push.

What this module pins:

* The decision: a non-window branch, no open window, a conflicting window, a
  head past the hold, and an unreadable probe all proceed at once. Running
  check runs, a young head with no runs yet, and an armed window with no red
  run all wait. A window that merged or closed is gone.
* The wait loop: it proceeds once the window is quiet, proceeds at the
  timeout, exits ``EXIT_WINDOW_GONE`` when the window merges while it waits,
  and proceeds when a later probe cannot read the window.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO_ROOT / "scripts" / "ci"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import occ_window_quiet_wait as guard  # noqa: E402

pytestmark = pytest.mark.unit

WINDOW = "auto/window-omninode-ai-omnimarket-occ-autobind"
AUTOBIND = "auto/omninode-ai-omnimarket-pr-3130-occ-autobind"
HEAD = "5e1f00d2" + "0" * 32


def _run(status: str, conclusion: str | None) -> guard.CheckRun:
    return guard.CheckRun(name="ci", status=status, conclusion=conclusion)


def _window(
    *,
    runs: tuple[guard.CheckRun, ...] = (),
    head_age: float = 600.0,
    armed: bool = False,
    conflicting: bool = False,
    state: str = "open",
    merged: bool = False,
) -> guard.WindowSnapshot:
    return guard.WindowSnapshot(
        number=7001,
        state=state,
        merged=merged,
        conflicting=conflicting,
        head_sha=HEAD,
        head_age_seconds=head_age,
        armed=armed,
        check_runs=runs,
    )


GREEN = (_run("completed", "success"), _run("completed", "skipped"))
RED = (_run("completed", "success"), _run("completed", "failure"))
RUNNING = (_run("completed", "success"), _run("in_progress", None))


# --- constants -------------------------------------------------------------


def test_constants_match_the_emitter_guard() -> None:
    assert guard.WINDOW_MAX_HOLD_SECONDS == 1800
    assert guard.WINDOW_SETTLE_SECONDS == 180
    assert guard.EXIT_PROCEED == 0
    assert guard.EXIT_WINDOW_GONE == 10
    assert {"failure", "timed_out", "cancelled"} <= guard.RED_CHECK_CONCLUSIONS


def test_window_branch_detection() -> None:
    assert guard.is_window_branch(WINDOW)
    assert not guard.is_window_branch(AUTOBIND)
    assert not guard.is_window_branch("auto/ticket-omn-20042-occ-autobind")


# --- the pure decision -----------------------------------------------------


def test_non_window_branch_proceeds_whatever_the_window_reads() -> None:
    decision = guard.decide(AUTOBIND, _window(runs=RUNNING))
    assert decision.verdict is guard.EnumQuietVerdict.PROCEED


def test_no_open_window_proceeds() -> None:
    decision = guard.decide(WINDOW, guard.NoOpenWindow())
    assert decision.verdict is guard.EnumQuietVerdict.PROCEED


def test_in_flight_window_waits() -> None:
    decision = guard.decide(WINDOW, _window(runs=RUNNING))
    assert decision.verdict is guard.EnumQuietVerdict.WAIT
    assert "running" in decision.reason


def test_young_head_with_no_runs_waits() -> None:
    decision = guard.decide(WINDOW, _window(runs=(), head_age=60))
    assert decision.verdict is guard.EnumQuietVerdict.WAIT


def test_settled_head_with_no_runs_proceeds() -> None:
    decision = guard.decide(WINDOW, _window(runs=(), head_age=240))
    assert decision.verdict is guard.EnumQuietVerdict.PROCEED


def test_armed_and_green_waits_because_it_is_merging() -> None:
    decision = guard.decide(WINDOW, _window(runs=GREEN, armed=True))
    assert decision.verdict is guard.EnumQuietVerdict.WAIT
    assert "armed" in decision.reason


def test_armed_but_red_proceeds() -> None:
    decision = guard.decide(WINDOW, _window(runs=RED, armed=True))
    assert decision.verdict is guard.EnumQuietVerdict.PROCEED


def test_unarmed_quiet_window_proceeds() -> None:
    decision = guard.decide(WINDOW, _window(runs=GREEN))
    assert decision.verdict is guard.EnumQuietVerdict.PROCEED


def test_stale_head_proceeds_even_while_running() -> None:
    decision = guard.decide(WINDOW, _window(runs=RUNNING, head_age=1801))
    assert decision.verdict is guard.EnumQuietVerdict.PROCEED
    assert "1800" in decision.reason


def test_conflicting_window_proceeds() -> None:
    decision = guard.decide(WINDOW, _window(runs=RUNNING, conflicting=True))
    assert decision.verdict is guard.EnumQuietVerdict.PROCEED


def test_unreadable_probe_proceeds() -> None:
    decision = guard.decide(WINDOW, guard.ProbeFailed(reason="HTTP 403"))
    assert decision.verdict is guard.EnumQuietVerdict.PROCEED
    assert "HTTP 403" in decision.reason


@pytest.mark.parametrize(("state", "merged"), [("closed", True), ("closed", False)])
def test_merged_or_closed_window_is_gone(state: str, merged: bool) -> None:
    decision = guard.decide(WINDOW, _window(state=state, merged=merged))
    assert decision.verdict is guard.EnumQuietVerdict.WINDOW_GONE


# --- the wait loop ---------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _ScriptedProbe:
    """Returns the scripted observations in order, repeating the last one."""

    def __init__(self, *observations: guard.Observation) -> None:
        self._observations = list(observations)
        self.calls: list[tuple[str, int | None]] = []

    def observe(self, branch: str, number: int | None) -> guard.Observation:
        self.calls.append((branch, number))
        if len(self._observations) > 1:
            return self._observations.pop(0)
        return self._observations[0]


def _wait(
    probe: _ScriptedProbe, clock: _FakeClock, *, max_wait_seconds: float = 1500
) -> int:
    lines: list[str] = []
    code = guard.wait_for_quiet(
        WINDOW,
        probe,
        max_wait_seconds=max_wait_seconds,
        poll_seconds=30,
        clock=clock.time,
        sleep=clock.sleep,
        log=lines.append,
    )
    assert lines, "every exit path says why"
    return code


def test_loop_does_not_probe_a_non_window_branch() -> None:
    probe = _ScriptedProbe(_window(runs=RUNNING))
    clock = _FakeClock()
    lines: list[str] = []
    code = guard.wait_for_quiet(
        AUTOBIND,
        probe,
        max_wait_seconds=1500,
        poll_seconds=30,
        clock=clock.time,
        sleep=clock.sleep,
        log=lines.append,
    )
    assert code == guard.EXIT_PROCEED
    assert probe.calls == []
    assert clock.sleeps == []


def test_loop_proceeds_at_once_on_a_quiet_window() -> None:
    probe = _ScriptedProbe(_window(runs=GREEN))
    clock = _FakeClock()
    assert _wait(probe, clock) == guard.EXIT_PROCEED
    assert clock.sleeps == []


def test_loop_waits_until_the_window_goes_quiet() -> None:
    probe = _ScriptedProbe(
        _window(runs=RUNNING), _window(runs=RUNNING), _window(runs=GREEN)
    )
    clock = _FakeClock()
    assert _wait(probe, clock) == guard.EXIT_PROCEED
    assert clock.sleeps == [30, 30]
    # The first probe finds the open window by branch; the rest read that PR.
    assert probe.calls[0] == (WINDOW, None)
    assert probe.calls[1:] == [(WINDOW, 7001), (WINDOW, 7001)]


def test_loop_exits_window_gone_when_it_merges_while_waiting() -> None:
    probe = _ScriptedProbe(
        _window(runs=GREEN, armed=True), _window(state="closed", merged=True)
    )
    clock = _FakeClock()
    assert _wait(probe, clock) == guard.EXIT_WINDOW_GONE


def test_loop_proceeds_at_the_timeout() -> None:
    probe = _ScriptedProbe(_window(runs=RUNNING))
    clock = _FakeClock()
    assert _wait(probe, clock, max_wait_seconds=120) == guard.EXIT_PROCEED
    assert sum(clock.sleeps) <= 120
    assert len(clock.sleeps) == 4


def test_loop_proceeds_when_a_later_probe_is_unreadable() -> None:
    probe = _ScriptedProbe(_window(runs=RUNNING), guard.ProbeFailed(reason="boom"))
    clock = _FakeClock()
    assert _wait(probe, clock) == guard.EXIT_PROCEED


def test_loop_with_zero_budget_never_sleeps() -> None:
    probe = _ScriptedProbe(_window(runs=RUNNING))
    clock = _FakeClock()
    assert _wait(probe, clock, max_wait_seconds=0) == guard.EXIT_PROCEED
    assert clock.sleeps == []


def test_main_without_a_token_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCC_TOKEN", raising=False)
    assert guard.main(["--branch", WINDOW]) == guard.EXIT_PROCEED
